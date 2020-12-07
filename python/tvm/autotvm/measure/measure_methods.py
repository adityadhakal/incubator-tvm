# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name,too-many-function-args,too-many-nested-blocks
"""
Functions that run on executor for measurement.

These functions are responsible for building the tvm module, uploading it to
remote devices, recording the running time costs, and checking the correctness of the output.
"""

import logging
import shutil
import os
import threading
import time
from random import getrandbits
from collections import namedtuple
import tempfile

import numpy as np

import tvm._ffi
import tvm.ir.transform
from tvm import nd, rpc as _rpc
from tvm.target import Target
from tvm.error import TVMError
from tvm.driver import build
from tvm.contrib import nvcc, ndk, tar

from ..utils import get_const_tuple
from ..env import AutotvmGlobalScope
from ..task.space import InstantiationError

from .measure import MeasureResult, MeasureErrorNo, Builder, Runner
from .local_executor import LocalExecutor

logger = logging.getLogger("autotvm")


class BuildResult(namedtuple("BuildResult", ("filename", "arg_info", "error", "time_cost"))):
    """
    Stores all the necessary inputs for a measurement.

    Parameters
    ----------
    filename : str
        The filename of generated library
    arg_info : Tuple
        The shape and dtype information of tvm tensor arguments
    error : Exception
        The error happens during compilation.
    time_cost : float
        The time cost of building
    """


class LocalBuilder(Builder):
    """Run compilation on local machine

    Parameters
    ----------
    timeout: float
        The timeout of a compilation
    n_parallel: int
        The number of tasks run in parallel. "None" will use all cpu cores
    build_func: callable or str
        If is 'default', use default build function
        If is 'ndk', use function for android ndk
        If is callable, use it as custom build function, expect lib_format field.
    """

    def __init__(self, timeout=10, n_parallel=None, build_func="default"):
        super(LocalBuilder, self).__init__(timeout, n_parallel)

        if isinstance(build_func, str):
            if build_func == "default":
                build_func = tar.tar
            elif build_func == "ndk":
                build_func = ndk.create_shared
            else:
                raise ValueError("Invalid build_func" + build_func)
        self.build_func = _WrappedBuildFunc(build_func)
        self.executor = LocalExecutor(timeout=timeout)
        self.tmp_dir = tempfile.mkdtemp()

    def build(self, measure_inputs):
        print("\n*******------ Building Started ------*********")
        start_building = time.monotonic()
        results = []

        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir = tempfile.mkdtemp()

        for i in range(0, len(measure_inputs), self.n_parallel):
            futures = []
            for inp in measure_inputs[i : i + self.n_parallel]:
                ret = self.executor.submit(self.build_func, inp, self.tmp_dir, **self.build_kwargs)
                futures.append(ret)

            for future in futures:
                res = future.get()

                if isinstance(res, Exception):
                    # timeout or fleet error, return MeasureResult directly
                    results.append(
                        MeasureResult(
                            (res,), MeasureErrorNo.BUILD_TIMEOUT, self.timeout, time.time()
                        )
                    )
                elif res.error is not None:
                    # instantiation error
                    if isinstance(res.error, InstantiationError):
                        results.append(
                            MeasureResult(
                                (res.error,),
                                MeasureErrorNo.INSTANTIATION_ERROR,
                                res.time_cost,
                                time.time(),
                            )
                        )
                    else:
                        if "InstantiationError" in str(res.error):
                            msg = str(res.error)
                            try:
                                msg = msg.split("\n")[-2].split(": ")[1]
                            except Exception:  # pylint: disable=broad-except
                                pass
                            results.append(
                                MeasureResult(
                                    (InstantiationError(msg),),
                                    MeasureErrorNo.INSTANTIATION_ERROR,
                                    res.time_cost,
                                    time.time(),
                                )
                            )
                        else:  # tvm error
                            results.append(
                                MeasureResult(
                                    (res.error,),
                                    MeasureErrorNo.COMPILE_HOST,
                                    res.time_cost,
                                    time.time(),
                                )
                            )
                else:
                    # return BuildResult
                    results.append(res)

        end_building = time.monotonic()
        print("**** Total time taken to building: ***** : ",(end_building-start_building))
        return results


class RPCRunner(Runner):
    """Run generated code on remove devices.
    This function will ask a RPC Tracker to get device for measurement.

    Parameters
    ----------
    timeout: float
        The timeout of a RPCRunner measurement task
    n_parallel: int
        The number of tasks run in parallel. "None" will use all cpu cores
    key: str
        The key of the device registered in the tracker
    host: str
        The host address of RPC Tracker
    port: int
        The port of RPC Tracker
    number: int
        The number of times to run the generated code for taking average.
        We call these runs as one `repeat` of measurement.
    repeat : int, optional
        The number of times to repeat the measurement.
        In total, the generated code will be run (1 + number x repeat) times,
        where the first "1" is warm up and will be discarded.
        The returned result contains `repeat` costs,
        each of which is an average of `number` costs.
    min_repeat_ms: int, optional
        The minimum duration of one `repeat` in milliseconds.
        By default, one `repeat` contains `number` runs. If this parameter is set,
        the parameters `number` will be dynamically adjusted to meet the
        minimum duration requirement of one `repeat`.
        i.e., When the run time of one `repeat` falls below this time, the `number` parameter
        will be automatically increased.
    cooldown_interval: float, optional
        The cool down interval between two measurements.
    check_correctness: bool, optional
        Whether check correctness after measurement. This will use llvm cpu target to
        call your template and get the reference output.
        This can work for TOPI templates, but may not work for your custom template.
    enable_cpu_cache_flush: bool
        Whether to flush cache on CPU between repeated measurements.
        Flushing cache can make the measured latency of one operator closer to
        its actual latency during end-to-end inference.
        To make this option effective, the argument `number` should also be set to 1.
        This is only has effect on CPU task.
    """

    def __init__(
        self,
        key,
        host,
        port,
        priority=1,
        timeout=10,
        n_parallel=None,
        number=4,
        repeat=3,
        min_repeat_ms=0,
        cooldown_interval=0.1,
        check_correctness=False,
        enable_cpu_cache_flush=False,
    ):
        super(RPCRunner, self).__init__(timeout, n_parallel)

        self.key = key
        self.host = host
        self.port = port
        self.priority = priority
        self.timeout = timeout

        self.number = number
        self.repeat = repeat
        self.min_repeat_ms = min_repeat_ms

        self.ref_input = None
        self.ref_output = None
        self.enable_cpu_cache_flush = enable_cpu_cache_flush
        self.check_correctness = check_correctness
        self.cooldown_interval = cooldown_interval

        self.executor = LocalExecutor(timeout=timeout * (self.n_parallel + 1))

    def set_task(self, task):
        self.task = task

        if check_remote(task.target, self.key, self.host, self.port):
            logger.info("Get devices for measurement successfully!")
        else:
            raise RuntimeError(
                "Cannot get remote devices from the tracker. "
                "Please check the status of tracker by "
                "'python -m tvm.exec.query_rpc_tracker --port [THE PORT YOU USE]' "
                "and make sure you have free devices on the queue status."
            )

        if self.check_correctness:
            # use llvm cpu to generate a reference input/output
            # this option works for tuning topi, but might not work for you custom op
            with Target("llvm"):
                s, arg_bufs = task.instantiate(task.config_space.get(0))
            self.ref_input = [
                np.random.uniform(size=get_const_tuple(x.shape)).astype(x.dtype) for x in arg_bufs
            ]
            func = build(s, arg_bufs, "llvm")
            tvm_buf = [nd.array(x) for x in self.ref_input]
            func(*tvm_buf)
            self.ref_output = [x.asnumpy() for x in tvm_buf]

    def get_build_kwargs(self):
        kwargs = {}
        if (
            "cuda" in self.task.target.keys
            or "opencl" in self.task.target.keys
            or "rocm" in self.task.target.keys
            or "vulkan" in self.task.target.keys
        ):
            remote = request_remote(self.key, self.host, self.port)
            ctx = remote.context(str(self.task.target), 0)
            max_dims = ctx.max_thread_dimensions
            kwargs["check_gpu"] = {
                "max_shared_memory_per_block": ctx.max_shared_memory_per_block,
                "max_threads_per_block": ctx.max_threads_per_block,
                "max_thread_x": max_dims[0],
                "max_thread_y": max_dims[1],
                "max_thread_z": max_dims[2],
            }

            if "cuda" in self.task.target.keys:
                kwargs["cuda_arch"] = "sm_" + "".join(ctx.compute_version.split("."))
        if self.task.target.device_name == "micro_dev":
            kwargs.setdefault("build_option", {})["tir.disable_vectorize"] = True

        return kwargs
    '''
    def run(self, measure_inputs, build_results):
        run_start = time.monotonic()
        results = []
        remote_args = (self.key, self.host, self.port, self.priority, self.timeout)

        for i in range(0, len(measure_inputs), self.n_parallel):
            futures = []
            for measure_inp, build_res in zip(
                measure_inputs[i : i + self.n_parallel], build_results[i : i + self.n_parallel]
            ):
                ret = self.executor.submit(
                    run_through_rpc,
                    measure_inp,
                    build_res,
                    self.number,
                    self.repeat,
                    self.min_repeat_ms,
                    self.cooldown_interval,
                    remote_args,
                    self.ref_input,
                    self.ref_output,
                    self.enable_cpu_cache_flush,
                )
                futures.append(ret)

            for future in futures:
                res = future.get()
                if isinstance(res, Exception):  # executor error or timeout
                    results.append(
                        MeasureResult(
                            (str(res),), MeasureErrorNo.RUN_TIMEOUT, self.timeout, time.time()
                        )
                    )
                else:
                    results.append(res)
        end_runtime = time.monotonic()
        print("time to run/profile: ",(end_runtime-run_start))
        return results
    '''
    def run(self, measure_inputs, build_results):
        run_start = time.monotonic()
        results = []
        remote_args = (self.key, self.host, self.port, self.priority, self.timeout)

        #the number of measure inputs for a client.
        measures_per_client = self.n_parallel

        #for i in range(0, len(measure_inputs), self.n_parallel):
        for i in range(0, len(measure_inputs), self.n_parallel):
            measures_per_client = len(measure_inputs[i:i+self.n_parallel])
            print("Number of Configuration to Profile: ",measures_per_client)
            bitmap = [0]*measures_per_client
            futures = [None]*measures_per_client
            number_of_results = 0
            futures_got = []
            repeat = 0
            #futures = []
            #for measure_inp, build_res in zip(
            #    measure_inputs[i : i + self.n_parallel], build_results[i : i + self.n_parallel]
            #):
            while number_of_results < measures_per_client:
                futures = self.executor.submit(
                    run_through_rpc_modified,
                    measure_inputs[i:i+measures_per_client],
                    build_results[i:i+measures_per_client],
                    self.number,
                    self.repeat,
                    self.min_repeat_ms,
                    self.cooldown_interval,
                    remote_args,
                    bitmap,
                    self.ref_input,
                    self.ref_output,
                    self.enable_cpu_cache_flush,
                )
                #futures = run_through_rpc_modified(
                #measure_inputs[i:i+measures_per_client],
                #build_results[i:i+measures_per_client],
                #self.number,
                #self.repeat,
                #self.min_repeat_ms,
                #self.cooldown_interval,
                #remote_args,
                #bitmap,
                #self.ref_input,
                #self.ref_output,
                #self.enable_cpu_cache_flush,
                #)
                
                #    futures.append(ret)
                futures_got = futures.get()
                
                if isinstance(futures_got, Exception):
                    if repeat == 0:
                        repeat = 1
                        print("Got an error before receiving profile of configuration. Repeat")
                        futures_got = []
                        bitmap = [0]*measures_per_client
                        #redo this once.
                        continue
                    else:
                        print("This has been repeated, we are going to accept the result this time")
                    #entire thing is thrown out
                else:
                    print("Size of Future: ", len(futures_got))

                    for j,future in enumerate(futures_got):
                        #res = future.get()
                        res = future
                        if isinstance(res, Exception):  # executor error or timeout
                            if bitmap[j]==1 or bitmap[j]==2: #only append if it has been tried before
                                results.append(
                                    MeasureResult(
                                        (str(res),), MeasureErrorNo.RUN_TIMEOUT, self.timeout, time.time()
                                    )
                                )
                                bitmap[j]=2
                                number_of_results = number_of_results+1
                            else:
                                bitmap[j] = 1
                        else:
                            number_of_results = number_of_results+1
                            bitmap[j] = 2                    
                            results.append(res)
                print("Bitmap in parent: ", bitmap)
                print("Number of final results: ",number_of_results)
                #make sure loop ends in 2nd round.
                if repeat == 1 and number_of_results < measures_per_client:
                    #makes sure the loop ends.
                    number_of_results = measures_per_client
        end_runtime = time.monotonic()
        print("time to run/profile: ",(end_runtime-run_start))
        return results
    

class LocalRunner(RPCRunner):
    """Run generated code on local devices.

    Parameters
    ----------
    timeout: float
        The timeout of a compilation
    number: int
        The number of times to run the generated code for taking average.
        We call these runs as one `repeat` of measurement.
    repeat : int, optional
        The number of times to repeat the measurement.
        In total, the generated code will be run (1 + number x repeat) times,
        where the first one is warm up and will be discarded.
        The returned result contains `repeat` costs,
        each of which is an average of `number` costs.
    min_repeat_ms: int, optional
        The minimum duration of one `repeat` in milliseconds.
        By default, one `repeat` contains `number` runs. If this parameter is set,
        the parameters `number` will be dynamically adjusted to meet the
        minimum duration requirement of one `repeat`.
        i.e., When the run time of one `repeat` falls below this time, the `number` parameter
        will be automatically increased.
    cooldown_interval: float, optional
        The cool down interval between two measurements.
    check_correctness: bool, optional
        Whether check correctness after measurement. This will use llvm cpu target to
        call your template and get the reference output.
        This can work for TOPI templates, but may not work for your custom template.
    enable_cpu_cache_flush: bool
        Whether to flush cache on CPU between repeated measurements.
        Flushing cache can make the measured latency of one operator closer to
        its actual latency during end-to-end inference.
        To make this option effective, the argument `number` should also be set to 1.
        This is only has effect on CPU task.
    Note
    ----
    This is a "fake" local mode. We start a silent rpc tracker and rpc server
    for the user. In this way we reuse timeout/isolation mechanism in RPC infrastructure.
    """

    def __init__(
        self,
        timeout=10,
        number=4,
        repeat=3,
        min_repeat_ms=0,
        cooldown_interval=0.1,
        check_correctness=False,
        enable_cpu_cache_flush=False,
    ):
        super(LocalRunner, self).__init__(
            "",
            None,
            None,
            0,
            timeout=timeout,
            n_parallel=1,
            number=number,
            repeat=repeat,
            min_repeat_ms=min_repeat_ms,
            cooldown_interval=cooldown_interval,
            check_correctness=check_correctness,
            enable_cpu_cache_flush=enable_cpu_cache_flush,
        )
        self.tracker = None
        self.server = None

    def set_task(self, task):
        # pylint: disable=import-outside-toplevel
        from ...rpc.tracker import Tracker
        from ...rpc.server import Server

        self.task = task
        tracker = Tracker("0.0.0.0", port=9000, port_end=10000, silent=True)
        device_key = "$local$device$%d" % tracker.port
        server = Server(
            "0.0.0.0",
            port=9000,
            port_end=10000,
            key=device_key,
            use_popen=True,
            silent=True,
            tracker_addr=(tracker.host, tracker.port),
        )
        self.key = device_key
        self.host = tracker.host
        self.port = tracker.port

        super(LocalRunner, self).set_task(task)
        return server, tracker


def _build_func_common(measure_input, check_gpu=None, cuda_arch=None, build_option=None):
    """Common part for building a configuration"""
    target, task, config = measure_input
    with target:
        s, args = task.instantiate(config)

        # check invalidity of template and code hash consistency
        if not config.valid():
            raise InstantiationError(config.errors)

        opts = build_option or {}
        if check_gpu:  # Add verify pass to filter out invalid configs in advance.
            opts["tir.add_lower_pass"] = [(2, gpu_verify_pass(**check_gpu))]
        if cuda_arch:
            set_cuda_target_arch(cuda_arch)

        # if target is vta, we need to use vta build
        if (
            hasattr(measure_input.target, "device_name")
            and measure_input.target.device_name == "vta"
        ):
            # pylint: disable=import-outside-toplevel
            import vta

            func = vta.build(s, args, target_host=task.target_host)
        else:
            with tvm.ir.transform.PassContext(config=opts):
                func = build(s, args, target_host=task.target_host)
    return func, tuple((get_const_tuple(x.shape), x.dtype) for x in args)


class _WrappedBuildFunc:
    """
    Wrap build_func to a function that can be used in measure.

    Note: this is a class instead of a closure so that it can be pickled when
    using multiprocessing.

    Parameters
    ----------
    build_func : The compilation function
        We expect fcompile to contain an attr "output_format"

    Returns
    -------
    wrapped_build_func : callable
        The wrapped build function
    """

    def __init__(self, build_func):
        if not hasattr(build_func, "output_format"):
            raise AttributeError("Expect build_func to have the attribute output_format.")
        self.build_func = build_func

    def __call__(self, measure_input, tmp_dir, **kwargs):
        """
        Wrapped build func.

        Parameters
        ----------
        measure_input: MeasureInput
            The input of measurement

        tmp_dir: str
            The path of temporary directory to export generated library
        """
        tic = time.time()
        try:
            filename = os.path.join(
                tmp_dir, "tmp_func_%0x.%s" % (getrandbits(64), self.build_func.output_format)
            )
            # TODO(tvm-team) consider linline _build_func_common
            func, arg_info = _build_func_common(measure_input, **kwargs)
            func.export_library(filename, self.build_func)
        except Exception as e:  # pylint: disable=broad-except
            return BuildResult(None, None, e, time.time() - tic)
        return BuildResult(filename, arg_info, None, time.time() - tic)


def run_through_rpc(
    measure_input,
    build_result,
    number,
    repeat,
    min_repeat_ms,
    cooldown_interval,
    remote_args,
    ref_input=None,
    ref_output=None,
    enable_cpu_cache_flush=False,
):
    """Run a generated library through rpc

    Parameters
    ----------
    measure_input: MeasureInput
        The raw measure input
    build_result: BuildResult
        The result returned from Builder. This contains the path to the generated library.
    number: int
        The number of times to run the generated code for taking average.
        We call these runs as one `repeat` of measurement.
    repeat : int, optional
        The number of times to repeat the measurement.
        In total, the generated code will be run (1 + number x repeat) times,
        where the first one is warm up and will be discarded.
        The returned result contains `repeat` costs,
        each of which is an average of `number` costs.
    min_repeat_ms: int, optional
        The minimum duration of one `repeat` in milliseconds.
        By default, one `repeat` contains `number` runs. If this parameter is set,
        the parameters `number` will be dynamically adjusted to meet the
        minimum duration requirement of one `repeat`.
        i.e., When the run time of one `repeat` falls below this time, the `number` parameter
        will be automatically increased.
    cooldown_interval: float
        The cool down interval between two measurements
    remote_args: Tuple
        The argument for request_remote
    ref_input: List of np.ndarray
        The reference input used for checking correctness
    ref_output: List of np.ndarray
        The reference output used for checking correctness
    enable_cpu_cache_flush: bool
        Whether to flush cache on CPU between repeated measurements.
        Flushing cache can make the measured latency of one operator closer to
        its actual latency during end-to-end inference.
        To make this option effective, the argument `number` should also be set to 1.
        This is only has effect on CPU task.
    """
    start_time = time.monotonic()
    print("---***---Start time for this instance:,",start_time)
    if isinstance(build_result, MeasureResult):
        return build_result

    tic = time.time()
    errno = MeasureErrorNo.NO_ERROR
    try:
        # upload built module
        start_remote = time.process_time()
        remote = request_remote(*remote_args)
        # Program the FPGA every single time when targeting VTA
        if (
            hasattr(measure_input.target, "device_name")
            and measure_input.target.device_name == "vta"
        ):
            # pylint: disable=import-outside-toplevel
            from vta import program_fpga, reconfig_runtime

            program_fpga(remote, None)
            reconfig_runtime(remote)
        end_remote = time.process_time()
        start_upload = time.process_time()
        remote.upload(build_result.filename)
        end_upload = time.process_time()
        print("Remote connection time: ",(end_remote-start_remote)," Upload time: ",(end_upload-start_upload))
        func = remote.load_module(os.path.split(build_result.filename)[1])
        ctx = remote.context(str(measure_input.target), 0)

        # Limitation:
        # We can not get PackFunction directly in the remote mode as it is wrapped
        # under the std::function. We could lift the restriction later once we fold
        # the PackedFunc as an object. Currently, we pass function name to work
        # around it.
        f_prepare = "cache_flush_cpu_non_first_arg" if enable_cpu_cache_flush else ""
        time_f = func.time_evaluator(
            func.entry_name,
            ctx,
            number=number,
            repeat=repeat,
            min_repeat_ms=min_repeat_ms,
            f_preproc=f_prepare,
        )

        # set input
        if ref_input:
            args = [nd.array(x, ctx=ctx) for x in ref_input]
        else:
            try:
                random_fill = remote.get_function("tvm.contrib.random.random_fill")
            except AttributeError:
                raise AttributeError(
                    "Please make sure USE_RANDOM is ON in the config.cmake " "on the remote devices"
                )
            args = [nd.empty(x[0], dtype=x[1], ctx=ctx) for x in build_result.arg_info]
            for arg in args:
                random_fill(arg)
            ctx.sync()

        costs = time_f(*args).results

        # clean up remote files
        remote.remove(build_result.filename)
        remote.remove(os.path.splitext(build_result.filename)[0] + ".so")
        remote.remove("")

        if len(costs) > 2:  # remove largest and smallest value to reduce variance
            costs = list(costs)
            costs.sort()
            costs = tuple(costs[1:-1])

        # check correctness of output
        if ref_output:
            for expected, real in zip(ref_output, args):
                if not np.allclose(expected, real.asnumpy(), rtol=1e-4):
                    logger.warning("Wrong Answer!")
                    errno = MeasureErrorNo.WRONG_ANSWER
    except TVMError as exc:
        msg = str(exc)
        if "Stack trace returned" in msg:
            msg = msg[: msg.index("Stack trace returned")]
        if "CUDA Source" in msg:
            msg = msg[: msg.index("CUDA Source")]
        costs = (RuntimeError(msg[:1024]),)
        errno = MeasureErrorNo.RUNTIME_DEVICE
    tstamp = time.time()
    time.sleep(cooldown_interval)
    start_time = time.monotonic()
    print("---***---End time for this instance:,",start_time)

    return MeasureResult(costs, errno, tstamp - tic + build_result.time_cost, tstamp)


def run_through_rpc_modified(
    measure_input,
    build_result,
    number,
    repeat,
    min_repeat_ms,
    cooldown_interval,
    remote_args,
        bitmap,
    ref_input=None,
    ref_output=None,
    enable_cpu_cache_flush=False,
):
    """Run a generated library through rpc

    Parameters
    ----------
    measure_input: MeasureInput
        The raw measure input
    build_result: BuildResult
        The result returned from Builder. This contains the path to the generated library.
    number: int
        The number of times to run the generated code for taking average.
        We call these runs as one `repeat` of measurement.
    repeat : int, optional
        The number of times to repeat the measurement.
        In total, the generated code will be run (1 + number x repeat) times,
        where the first one is warm up and will be discarded.
        The returned result contains `repeat` costs,
        each of which is an average of `number` costs.
    min_repeat_ms: int, optional
        The minimum duration of one `repeat` in milliseconds.
        By default, one `repeat` contains `number` runs. If this parameter is set,
        the parameters `number` will be dynamically adjusted to meet the
        minimum duration requirement of one `repeat`.
        i.e., When the run time of one `repeat` falls below this time, the `number` parameter
        will be automatically increased.
    cooldown_interval: float
        The cool down interval between two measurements
    remote_args: Tuple
        The argument for request_remote
    ref_input: List of np.ndarray
        The reference input used for checking correctness
    ref_output: List of np.ndarray
        The reference output used for checking correctness
    enable_cpu_cache_flush: bool
        Whether to flush cache on CPU between repeated measurements.
        Flushing cache can make the measured latency of one operator closer to
        its actual latency during end-to-end inference.
        To make this option effective, the argument `number` should also be set to 1.
        This is only has effect on CPU task.
    """
    #if isinstance(build_result, MeasureResult):
    #    return build_result
    start_time = time.monotonic()
    print("---***---Start time for this instance:,",start_time)

    results = []
    cost_list = []

    tic = time.time()
    errno = MeasureErrorNo.NO_ERROR
    print("***** Trying to Connect *******")
    #try:
    # upload built module
    start_remote = time.process_time()
    remote = request_remote(*remote_args)
    end_remote = time.process_time()
    # Program the FPGA every single time when targeting VTA
    if (
        hasattr(measure_input[0].target, "device_name")
        and measure_input[0].target.device_name == "vta"
    ):
        # pylint: disable=import-outside-toplevel
        from vta import program_fpga, reconfig_runtime

        program_fpga(remote, None)
        reconfig_runtime(remote)

    for i,build in enumerate(build_result):
        #ignore if it is already checked.
        if bitmap[i] == 2:
            continue
        
        if isinstance(build, MeasureResult):
            results.append(build)
            bitmap[i] = 2
            continue
        else:
            try:
                start_upload = time.process_time()
                remote.upload(build.filename)
                func = remote.load_module(os.path.split(build.filename)[1])
                ctx = remote.context(str(measure_input[i].target), 0)

                # Limitation:
                # We can not get PackFunction directly in the remote mode as it is wrapped
                # under the std::function. We could lift the restriction later once we fold
                # the PackedFunc as an object. Currently, we pass function name to work
                # around it.
                f_prepare = "cache_flush_cpu_non_first_arg" if enable_cpu_cache_flush else ""
                time_f = func.time_evaluator(
                    func.entry_name,
                    ctx,
                    number=number,
                    repeat=repeat,
                    min_repeat_ms=min_repeat_ms,
                    f_preproc=f_prepare,
                )

                # set input
                if ref_input:
                    args = [nd.array(x, ctx=ctx) for x in ref_input]
                else:
                    try:
                        random_fill = remote.get_function("tvm.contrib.random.random_fill")
                    except AttributeError:
                        raise AttributeError(
                            "Please make sure USE_RANDOM is ON in the config.cmake " "on the remote devices"
                        )
                    args = [nd.empty(x[0], dtype=x[1], ctx=ctx) for x in build.arg_info]
                    for arg in args:
                        random_fill(arg)
                        ctx.sync()

                costs = time_f(*args).results
                cost_list.append(costs)
                end_upload = time.process_time()
                print("Remote connection time: ",(end_remote-start_remote)," Upload time: ",(end_upload-start_upload))

                #update the bitmap
                bitmap[i] = 2

                # clean up remote files
                remote.remove(build.filename)
                remote.remove(os.path.splitext(build.filename)[0] + ".so")
                remote.remove("")
            except TVMError as exc:
                print("*****00000000 Exception Recorded ******&&&&***********---")
                bitmap[i] = 1 #1 means error
                msg = str(exc)
                if "Stack trace returned" in msg:
                    msg = msg[: msg.index("Stack trace returned")]
                if "CUDA Source" in msg:
                    msg = msg[: msg.index("CUDA Source")]
                costs = (RuntimeError(msg[:1024]),)
                errno = MeasureErrorNo.RUNTIME_DEVICE
                cost_list.append(costs)
                break #break the loop and send the result back. We need to make new connection
            
            
            time.sleep(cooldown_interval)

        for costs in cost_list:
            if len(costs) > 2:  # remove largest and smallest value to reduce variance
                costs = list(costs)
                costs.sort()
                costs = tuple(costs[1:-1])

        # check correctness of output
        if ref_output:
            for expected, real in zip(ref_output, args):
                if not np.allclose(expected, real.asnumpy(), rtol=1e-4):
                    logger.warning("Wrong Answer!")
                    errno = MeasureErrorNo.WRONG_ANSWER
    
    tstamp = time.time()
    counter2 = 0
    print("Size of cost list")
    print(len(cost_list))
    
    for i,build in enumerate(build_result):
        if bitmap[i] != 2:
            continue
        if isinstance(build, MeasureResult):
            #results.append(build)
            continue
        else:
            #return MeasureResult(costs, errno, tstamp - tic + build_result.time_cost, tstamp)
            print("Counter2 and i", counter2, i)
            if counter2<len(cost_list):
                #print(counter2)
                result = MeasureResult(cost_list[counter2], errno, tstamp - tic + build.time_cost, tstamp)
                results.append(result)
                counter2 = counter2 + 1

    #return MeasureResult(costs, errno, tstamp - tic + build_result.time_cost, tstamp)
    print("Bitmap in child: ",bitmap)
    start_time = time.monotonic()
    print("---***---End time for this instance:,",start_time)

    return results


def request_remote(device_key, host=None, port=None, priority=1, timeout=60):
    """Request a remote session

    Parameters
    ----------
    device_key: string
        The device key of registered device in tracker
    host: host, optional
        The host address of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_HOST"
    port: int, optional
        The port of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_PORT"
    priority: int, optional
        The priority of this request, larger is more prior
    timeout: float, optional
        The timeout of this session (units: second)

    Returns
    ------
    session: RPCSession
    """
    # connect to the tracker
    host = host or os.environ["TVM_TRACKER_HOST"]
    port = port or int(os.environ["TVM_TRACKER_PORT"])

    tracker = _rpc.connect_tracker(host, port)
    remote = tracker.request(device_key, priority=priority, session_timeout=timeout)
    return remote


def check_remote(target, device_key, host=None, port=None, priority=100, timeout=10):
    """
    Check the availability of a remote device

    Parameters
    ----------
    target: Target
        The wanted compilation target
    device_key: string
        device key of registered device in tracker
    host: host, optional
        The host address of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_HOST"
    port: int, optional
        The port address of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_PORT"
    priority: int, optional
        The priority of this request, larger is more prior
    timeout: float, optional
        The timeout of this check (units: seconds).

    Returns
    -------
    available: bool
        True if can find available device
    """

    print("check_remote timeout: ",timeout)
    print("device_key, host, port: ",device_key,host,port)
    def _check():
        remote = request_remote(device_key, host, port, priority)
        ctx = remote.context(str(target))
        while not ctx.exist:  # wait until we get an available device
            pass

    t = threading.Thread(
        target=_check,
    )
    t.start()
    t.join(timeout)
    return not t.is_alive()


@tvm._ffi.register_func
def tvm_callback_cuda_compile(code):
    """use nvcc to generate ptx code for better optimization"""
    curr_cuda_target_arch = AutotvmGlobalScope.current.cuda_target_arch
    # e.g., target arch could be [
    #   "-gencode", "arch=compute_52,code=sm_52",
    #   "-gencode", "arch=compute_70,code=sm_70"
    # ]
    target = "fatbin" if isinstance(curr_cuda_target_arch, list) else "ptx"
    ptx = nvcc.compile_cuda(code, target=target, arch=AutotvmGlobalScope.current.cuda_target_arch)
    return ptx


def set_cuda_target_arch(arch):
    """set target architecture of nvcc compiler

    Parameters
    ----------
    arch: str or list
        The argument of nvcc -arch. (e.g. "sm_51", "sm_62")
        it can also be a count of gencode arguments pass to nvcc command line,
        e.g., ["-gencode", "arch=compute_52,code=sm_52", "-gencode", "arch=compute_70,code=sm_70"]
    """
    AutotvmGlobalScope.current.cuda_target_arch = arch


def gpu_verify_pass(**kwargs):
    """Verify the validity of a gpu kernel.
    This pass will check memory usage and number of threads per block.
    """

    def verify_pass(f, *_):
        valid = tvm.tir.analysis.verify_gpu_code(f, kwargs)
        if not valid:
            raise InstantiationError("Skipped because of invalid gpu kernel")
        return f

    return tvm.tir.transform.prim_func_pass(verify_pass, opt_level=0)

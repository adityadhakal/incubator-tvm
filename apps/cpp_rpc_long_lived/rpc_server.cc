/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

/*!
 * \file rpc_server.cc
 * \brief RPC Server implementation.
 */
//#define __linux__

//#define FORK_METHOD

#include <tvm/runtime/registry.h>

#if defined(__linux__) || defined(__ANDROID__)
#include <sys/select.h>
#include <sys/wait.h>
#endif
#include <set>
#include <iostream>
#include <future>
#include <thread>
#include <chrono>
#include <string>

#include <cuda_runtime.h>
#include <cuda.h>

#include "rpc_server.h"
#include "rpc_env.h"
#include "rpc_tracker_client.h"
#include "../../src/runtime/rpc/rpc_session.h"
#include "../../src/runtime/rpc/rpc_socket_impl.h"
#include "../../src/common/socket.h"

namespace tvm {
namespace runtime {

/*!
 * \brief wait the child process end.
 * \param status status value
 */
#if defined(__linux__) || defined(__ANDROID__)
__attribute__((unused)) static pid_t waitPidEintr(int *status) {
  pid_t pid = 0;
  while ((pid = waitpid(-1, status, 0)) == -1) {
    if (errno == EINTR) {
      continue;
    } else {
      perror("waitpid");
      abort();
    }
  }
  return pid;
}
#endif

/*!
 * \brief RPCServer RPC Server class.
 * \param host The hostname of the server, Default=0.0.0.0
 * \param port The port of the RPC, Default=9090
 * \param port_end The end search port of the RPC, Default=9199
 * \param tracker The address of RPC tracker in host:port format e.g. 10.77.1.234:9190 Default=""
 * \param key The key used to identify the device type in tracker. Default=""
 * \param custom_addr Custom IP Address to Report to RPC Tracker. Default=""
 */
class RPCServer {
 public:
  /*!
   * \brief Constructor.
  */
  RPCServer(const std::string &host,
            int port,
            int port_end,
            const std::string &tracker_addr,
            const std::string &key,
            const std::string &custom_addr) {
    // Init the values
    host_ = host;
    port_ = port;
    port_end_ = port_end;
    tracker_addr_ = tracker_addr;
    key_ = key;
    custom_addr_ = custom_addr;
    //zero the count instances variable
    count_instances = 0;
    gpu_reset_count = 0;
  }

  /*!
   * \brief Destructor.
  */
  ~RPCServer() {
    // Free the resources
    tracker_sock_.Close();
    listen_sock_.Close();
  }

  /*!
   * \brief Start Creates the RPC listen process and execution.
  */
  void Start() {
			  listen_sock_.Create();
			  my_port_ = listen_sock_.TryBindHost(host_, port_, port_end_);
			  LOG(INFO) << "bind to " << host_ << ":" << my_port_;
			  if(my_port_ == -1)
			  {
				  //failure to bind... exit the process
				  exit(0);
			  }
			  listen_sock_.Listen(1);
			  /* why don't we rather fork the process and wait for the result */
			  std::future<void> proc(std::async(std::launch::async, &RPCServer::ListenLoopProc, this));
			  proc.get();

			  // Close the listen socket
			  listen_sock_.Close();
  }

 private:
  int count_instances; //private variable to count instances
  int gpu_reset_count; //counting number of time GPU is reset
  /*!
   * \brief ListenLoopProc The listen process.
   */
  void ListenLoopProc() {
    TrackerClient tracker(tracker_addr_, key_, custom_addr_);
    pid_t mps_server_pid, prev_server_pid;
    FILE *cmd2;
    char *fget_ret;
    char mps_server_pid_s[1024];
    prev_server_pid = 0;

    //checking number of SMs to verify we are applying right percentage.
    //int num_sms;
    //cudaDeviceGetAttribute(&num_sms,cudaDevAttrMultiProcessorCount,0);

    while (true) {
      common::TCPSocket conn;
      common::SockAddr addr("0.0.0.0", 0);
      std::string opts;
      try {
        // step 1: setup tracker and report to tracker
        tracker.TryConnect();
#ifndef FORK_METHOD
        // step 2: wait for in-coming connections
        AcceptConnection(&tracker, &conn, &addr, &opts);
#endif //ifndef FORK_METHOD
      }
      catch (const char* msg) {
        LOG(WARNING) << "Socket exception: " << msg;
        // close tracker resource
        tracker.Close();
        continue;
      }
      catch (std::exception& e) {
        // Other errors
        LOG(WARNING) << "Exception standard: " << e.what();
        continue;
      }

#ifdef FORK_METHOD
      //Fork the process to do rest of the thing here.
      pid_t child_pid = fork();
      if(child_pid == 0){
    	  while(true){
    		  //first accept the connection
    		  try{
    			  //Step 2: wait for in-coming connections
    			  AcceptConnection(&tracker, &conn, &addr, &opts);
    		  }
    		  catch (const char * msg){
    			  LOG(WARNING) <<"Socket exception: "<<msg;
    			  tracker.Close();
    			  //exit the fork
    			  LOG(INFO)<<"Cannot Reach the Tracker.. quitting the child";
    			  //_exit(0);
    			  continue;
    		  }
    		  catch (std::exception& e) {
    			  // Other errors
    			  LOG(WARNING) << "Exception standard: " << e.what();
    			  continue;
    		  }

#endif //FORK_METHOD
    		  __attribute__((unused)) int timeout = GetTimeOutFromOpts(opts);
				#if defined(__linux__) || defined(__ANDROID__)

    		  //code that checks if CUDA call returns in time
    		  std::future<cudaError_t> cuda_call(std::async(std::launch::async,&cudaGetLastError));

    		  std::future_status status;
    		  status = cuda_call.wait_for(std::chrono::seconds(5));
    		  if(status == std::future_status::timeout){
    			  LOG(INFO)<<"CUDA CALL TIMEOUT";
    			  //Trying to find the PID of CUDA MPS SERVER
    			  cmd2 = popen("pidof nvidia-cuda-mps-server", "r");
    			  fget_ret = fgets(mps_server_pid_s, 1024, cmd2);
    			  if(fget_ret == NULL){
    				  printf("Reading PID of nvidia-cuda-mps-server did not work.\n");
    			  }
    			  //Convert the string to pid
    			  pid_t mps_server_pid2 = strtoul(mps_server_pid_s, NULL, 10);
    			  pclose(cmd2);

    			  //Prevent killing the process 0.
    			  if(mps_server_pid2>0){
    				  LOG(INFO)<<"MPS server "<<mps_server_pid2<<" has been killed.";
    				  kill(mps_server_pid2, 9); //kill the MPS server before quitting
    			  }
    			  exit(0);
    		  }

    		  cudaError_t cuda_error = cuda_call.get();
    		  LOG(INFO)<<"CUDA error before loading module "<<cuda_error;
    		  size_t gpu_free, total_gpu, gpu_occupied;

    		  cudaError_t errval = cudaMemGetInfo(&gpu_free, &total_gpu);
    		  if (errval != cudaSuccess) {
    			  LOG(INFO)<<"Error Getting GPU Mem. Error: "<<errval<<" Resetting and Checking again.";
    			  cudaDeviceReset();
    			  //Aditya's code that checks if CUDA call returns in time
    			  std::future<cudaError_t> cuda_call2(std::async(std::launch::async,&cudaGetLastError));
    			  std::future_status  status2;
    			  status2 = cuda_call2.wait_for(std::chrono::seconds(5));
    			  if(status2 == std::future_status::timeout){
    				  //  The problem seems to persist seriously that we cannot launch any CUDA functions
    				  exit(0);
    			  }

    			  cudaError_t errval2 = cuda_call2.get();
    			  //if the error still persist
    			  if(errval2 != cudaSuccess){
    				  LOG(INFO) << "Socket Connection Closed";
    				  conn.Close();
    				  exit(0);
    			  }
    		  }
    		  gpu_occupied = total_gpu - gpu_free;
    		  LOG(INFO)<<"GPU Memory: Total: "<<(total_gpu/(1024.0*1024*1024))<<" Free: "<<(gpu_free/(1024.0*1024*1024))<<" Occupied Mem: "<<(gpu_occupied/(1024.0*1024*1024))<<" Time GPU is Reset: "<<gpu_reset_count;
    		  if (gpu_occupied > total_gpu / 2) {
    			  gpu_reset_count++;
    			  LOG(INFO)<<"More than quarter of GPU memory exceeded. Resetting GPU context. Number of time context is reset: "<<gpu_reset_count;
    			  cudaDeviceReset();
    			  //now wait till other process have also freed the memory
    			  int counter = 0;
    			  while(gpu_occupied> total_gpu/2){
    				  sleep(1); //check for 3 seconds
    				  errval = cudaMemGetInfo(&gpu_free, &total_gpu);
    				  gpu_occupied = total_gpu-gpu_free;
    				  counter++;
    				  if(counter >3){
    					  counter = 0;
    					  cudaDeviceReset();
    				  }
    			  }
    		  }

    		  count_instances++;
    		  /* rather than launching like this... we should launch with async*/
    		  ServerLoopProc(conn,addr);
    		  conn.Close();
    		  LOG(INFO)<<"Socket Closed";

    		  cudaError_t last_error = cudaGetLastError();
    		  //cuCtxDestroy(gpu_context); //destroying the GPU context
    		  LOG(INFO)<<"Last CUDA ERROR: "<<last_error;
    		  if (last_error != cudaSuccess) {
    			  LOG(INFO)<<"Restarting the process due to GPU error: ";
    			  //LOG(INFO) << "Socket Connection Closed";
    			  //conn.Close();
    			  exit(0);
    		  }
    		  LOG(INFO) <<"Counting the number of instances Tuned till now: "<<count_instances;// <<" Multiprocessor count: "<<num_sms;

    		  //similarly check if GPU memory occupancy is high. Reset if it is.
    		  errval = cudaMemGetInfo(&gpu_free, &total_gpu);
    		  gpu_occupied = total_gpu-gpu_free;
    		  if (gpu_occupied > total_gpu / 2) {
    			  gpu_reset_count++;
    			  LOG(INFO)<<"More than half of GPU memory exceeded. Resetting GPU context. Number of time context is reset: "<<gpu_reset_count;
    			  cudaDeviceReset();
    		  }

    		  //char mps_server_pid_s[1024];
    		  cmd2 = popen("pidof nvidia-cuda-mps-server", "r");
    		  fget_ret = fgets(mps_server_pid_s, 1024, cmd2);
    		  if(fget_ret == NULL){
    			  printf("Reading PID of nvidia-cuda-mps-server did not work.\n");
    		  }
    		  mps_server_pid = strtoul(mps_server_pid_s, NULL, 10);
    		  pclose(cmd2);
    		  if(prev_server_pid != 0){
    			  if(mps_server_pid != prev_server_pid){
    				  LOG(INFO)<<"MPS server change detected.";
    				  //conn.Close();
    				  exit(0);
    			  }
    		  }
    		  else
    		  {
    			  prev_server_pid = mps_server_pid;
    		  }


#ifdef FORK_METHOD
    	  }//end of inside while
    	  _exit(0); //end the child process
      }
      //Wait for the fork process and if it has crashed recover
      int status;
      static int reset_count = 0;
      wait(&status);
      reset_count++;
      LOG(INFO)<<"Child process crashed. Status: "<<status<<" The number of time it has crashed : "<<reset_count;
#endif //FORKMETHOD

      //CODE CHANGED TO REMOVE FORKING REQUIREMENT
      /*
        // step 3: serving
        if (timeout != 0) {
          const pid_t timer_pid = fork();
          if (timer_pid == 0) {
            // Timer process
            sleep(timeout);
            exit(0);
          }

          const pid_t worker_pid = fork();
          if (worker_pid == 0) {
            // Worker process
            ServerLoopProc(conn, addr);
            exit(0);
          }

          int status = 0;
          const pid_t finished_first = waitPidEintr(&status);
          if (finished_first == timer_pid) {
            kill(worker_pid, SIGKILL);
          } else if (finished_first == worker_pid) {
            kill(timer_pid, SIGKILL);
          } else {
            LOG(INFO) << "Child pid=" << finished_first << " unexpected, but still continue.";
          }

          int status_second = 0;
          waitPidEintr(&status_second);

          // Logging.
          if (finished_first == timer_pid) {
            LOG(INFO) << "Child pid=" << worker_pid << " killed (timeout = " << timeout
                      << "), Process status = " << status_second;
          } else if (finished_first == worker_pid) {
            LOG(INFO) << "Child pid=" << timer_pid << " killed, Process status = " << status_second;
          }
        } else {
          auto pid = fork();
          if (pid == 0) {
            ServerLoopProc(conn, addr);
            exit(0);
          }
          // Wait for the result
          int status = 0;
          wait(&status);
          LOG(INFO) << "Child pid=" << pid << " exited, Process status =" << status;

        }
        */
      #else
        // step 3: serving
        std::future<void> proc(std::async(std::launch::async,
                                          &RPCServer::ServerLoopProc, this, conn, addr));
        // wait until server process finish or timeout
        if (timeout != 0) {
          // Autoterminate after timeout
          proc.wait_for(std::chrono::seconds(timeout));
        } else {
          // Wait for the result
          proc.get();
        }
      #endif
      // close from our side.
      //LOG(INFO) << "Socket Connection Closed";
      //conn.Close();
    }
  }

  /*!
   * \brief AcceptConnection Accepts the RPC Server connection.
   * \param tracker Tracker details.
   * \param conn New connection information.
   * \param addr New connection address information.
   * \param opts Parsed options for socket
   * \param ping_period Timeout for select call waiting
   */
void AcceptConnection(TrackerClient* tracker,
                        common::TCPSocket* conn_sock,
                        common::SockAddr* addr,
                        std::string* opts,
                        int ping_period = 2) {
    std::set <std::string> old_keyset;
    std::string matchkey;

    // Report resource to tracker and get key
    tracker->ReportResourceAndGetKey(my_port_, &matchkey);

    while (true) {
      tracker->WaitConnectionAndUpdateKey(listen_sock_, my_port_, ping_period, &matchkey);
      common::TCPSocket conn = listen_sock_.Accept(addr);

      int code = kRPCMagic;
      CHECK_EQ(conn.RecvAll(&code, sizeof(code)), sizeof(code));
      if (code != kRPCMagic) {
        conn.Close();
        LOG(FATAL) << "Client connected is not TVM RPC server";
        continue;
      }

      int keylen = 0;
      CHECK_EQ(conn.RecvAll(&keylen, sizeof(keylen)), sizeof(keylen));

      const char* CLIENT_HEADER = "client:";
      const char* SERVER_HEADER = "server:";
      std::string expect_header = CLIENT_HEADER + matchkey;
      std::string server_key = SERVER_HEADER + key_;
      if (size_t(keylen) < expect_header.length()) {
        conn.Close();
        LOG(INFO) << "Wrong client header length";
        continue;
      }

      CHECK_NE(keylen, 0);
      std::string remote_key;
      remote_key.resize(keylen);
      CHECK_EQ(conn.RecvAll(&remote_key[0], keylen), keylen);

      std::stringstream ssin(remote_key);
      std::string arg0;
      ssin >> arg0;
      if (arg0 != expect_header) {
          code = kRPCMismatch;
          CHECK_EQ(conn.SendAll(&code, sizeof(code)), sizeof(code));
          conn.Close();
          LOG(WARNING) << "Mismatch key from" << addr->AsString();
          continue;
      } else {
        code = kRPCSuccess;
        CHECK_EQ(conn.SendAll(&code, sizeof(code)), sizeof(code));
        keylen = server_key.length();
        CHECK_EQ(conn.SendAll(&keylen, sizeof(keylen)), sizeof(keylen));
        CHECK_EQ(conn.SendAll(server_key.c_str(), keylen), keylen);
        LOG(INFO) << "Connection success " << addr->AsString();
        ssin >> *opts;
        *conn_sock = conn;
        return;
      }
    }
  }

  /*!
   * \brief ServerLoopProc The Server loop process.
   * \param sock The socket information
   * \param addr The socket address information
   */
  void ServerLoopProc(common::TCPSocket sock, common::SockAddr addr) {
      // Server loop
      auto env = RPCEnv();
      RPCServerLoop(sock.sockfd);
      LOG(INFO) << "Finish serving " << addr.AsString();
      env.CleanUp();
  }

  /*!
   * \brief GetTimeOutFromOpts Parse and get the timeout option.
   * \param opts The option string
   * \param timeout value after parsing.
   */
  int GetTimeOutFromOpts(std::string opts) {
    std::string cmd;
    std::string option = "-timeout=";

    if (opts.find(option) == 0) {
      cmd = opts.substr(opts.find_last_of(option) + 1);
      CHECK(common::IsNumber(cmd)) << "Timeout is not valid";
      return std::stoi(cmd);
    }
    return 0;
  }

  std::string host_;
  int port_;
  int my_port_;
  int port_end_;
  std::string tracker_addr_;
  std::string key_;
  std::string custom_addr_;
  common::TCPSocket listen_sock_;
  common::TCPSocket tracker_sock_;
};

/*!
 * \brief RPCServerCreate Creates the RPC Server.
 * \param host The hostname of the server, Default=0.0.0.0
 * \param port The port of the RPC, Default=9090
 * \param port_end The end search port of the RPC, Default=9199
 * \param tracker The address of RPC tracker in host:port format e.g. 10.77.1.234:9190 Default=""
 * \param key The key used to identify the device type in tracker. Default=""
 * \param custom_addr Custom IP Address to Report to RPC Tracker. Default=""
 * \param silent Whether run in silent mode. Default=True
 */
void RPCServerCreate(std::string host,
                     int port,
                     int port_end,
                     std::string tracker_addr,
                     std::string key,
                     std::string custom_addr,
                     bool silent) {
  if (silent) {
    // Only errors and fatal is logged
    dmlc::InitLogging("--minloglevel=2");
  }
  // Start the rpc server
  RPCServer rpc(host, port, port_end, tracker_addr, key, custom_addr);
  rpc.Start();
}

TVM_REGISTER_GLOBAL("rpc._ServerCreate")
.set_body([](TVMArgs args, TVMRetValue* rv) {
    RPCServerCreate(args[0], args[1], args[2], args[3], args[4], args[5], args[6]);
  });
}  // namespace runtime
}  // namespace tvm

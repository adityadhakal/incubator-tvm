/*
 * orchestrator.cc
 *
 *  Created on: Apr 18, 2020
 *      Author: Aditya
 */
#include <stdio.h>
#include <unistd.h>
#include <sys/wait.h>
#include <limits.h>
#include <iostream>
#include<stdlib.h>
#define TWO_PROCESS
int main(void) {
	char cwd[PATH_MAX];
	if (getcwd(cwd, sizeof(cwd)) != NULL) {
		printf("Current working dir: %s\n", cwd);
	} else {
		perror("getcwd() error");
		return 1;
	}

	int proc1,proc2, proc3, proc4;
	proc1 = 1;//flag for proc 1; let it spawn the process first time
	proc2 = 1; //flag for proc 2; let it spawn the process first time
	#ifndef TWO_PROCESS
	proc3 = 1;// flag for proc 3;
	proc4 = 1; //flag for proc 4;
#endif //two_process
	pid_t child_pid, child_pid2, child_pid3, child_pid4;
	while(true){
		if(proc1){
			proc1 = 0;
			//fork the process first and run execve
			child_pid = fork();
			if(child_pid==0){
				//detatching the process
				int rc = setsid();
				//child process
				system("/home/adhak001/dev_tvm/aditya_tvm/apps/cpp_rpc/tvm_rpc server --port=9090 --port-end=9180 --tracker=192.168.0.90:9190 --key=aditya");
				_exit(0);
			}
		}
		//if 2nd process should be started/restarted
		if(proc2){
			proc2 = 0;
			//second fork
			child_pid2 = fork();
			if(child_pid2==0){
				int rc = setsid();
				//second child
				system("/home/adhak001/dev_tvm/aditya_tvm/apps/cpp_rpc/tvm_rpc server --port=9090 --port-end=9180 --tracker=192.168.0.90:9190 --key=aditya");
				_exit(0);
			}
		}
#ifndef TWO_PROCESS
		//if 3rd process should be started/restarted
		if(proc3){
			proc3 = 0;
			//second fork
			child_pid3 = fork();
			if(child_pid3==0){
				int rc = setsid();
				//second child
				system("/home/adhak001/dev_tvm/aditya_tvm/apps/cpp_rpc/tvm_rpc server --port=9090 --port-end=9180 --tracker=192.168.0.90:9190 --key=aditya");
				_exit(0);
			}
		}
		//if 2nd process should be started/restarted
		if(proc4){
			proc4 = 0;
			//second fork
			child_pid4 = fork();
			if(child_pid4==0){
				int rc = setsid();
				//second child
				system("/home/adhak001/dev_tvm/aditya_tvm/apps/cpp_rpc/tvm_rpc server --port=9090 --port-end=9180 --tracker=192.168.0.90:9190 --key=aditya");
				_exit(0);
			}
		}
#endif //TWO_PROCESS
		/* Below code uses waitpid to detect the child has quit. Detaching the child might be safer method as we don't have to call wait then */
		int status = 0;
		//wait(&status);
		pid_t return_pid1, return_pid2, return_pid3, return_pid4;
		//check if the process have exited without blocking
		return_pid1 = waitpid(child_pid,&status,WNOHANG);
		if(return_pid1 == child_pid){
			std::cout<<"Proc 1 has died, restarting it"<<std::endl;
			proc1 = 1;
		}
		return_pid2 = waitpid(child_pid2,&status,WNOHANG);
		if(return_pid2 == child_pid2)
		{
			std::cout<<"Proc 2 has died, restarting it"<<std::endl;
			proc2 = 1;
		}
#ifndef TWO_PROCESS
		return_pid3 = waitpid(child_pid3, &status, WNOHANG);
		if(return_pid3 == child_pid3){
			std::cout<<"Proc 3 has died, restarting it"<<std::endl;
			proc3 = 1;
		}
		return_pid4 = waitpid(child_pid4, &status, WNOHANG);
		if(return_pid4 == child_pid4){
			std::cout<<"Proc 4 has died, restarting it"<<std::endl;
			proc4 = 1;
		}
#endif //TWO_PROCESS


		/*
		int status = 0;
		waitpid(child_pid,&status,WNOHANG);
		waitpid(child_pid2,&status,WNOHANG);
		waitpid(child_pid3,&status,WNOHANG);
		waitpid(child_pid4,&status,WNOHANG);
		if(kill(child_pid, 0) != 0)
		{

			//proc 1 has died
			proc1 = 1;
			std::cout<<"Proc 1 has died, restarting it."<<std::endl;
		}
		if(kill(child_pid2, 0) != 0)
		{
			waitpid(child_pid2,&status,WNOHANG);
			//proc 2 has died
			proc2 = 1;
			std::cout<<"Proc 2 has died, restarting it."<<std::endl;
		}
		if(kill(child_pid3, 0) != 0)
		{

			//proc 3 has died
			proc3 = 1;
			std::cout<<"Proc 3 has died, restarting it."<<std::endl;
		}
		if(kill(child_pid4, 0) != 0)
		{

			//proc 4 has died
			proc4 = 1;
			std::cout<<"Proc 4 has died, restarting it."<<std::endl;
		}
*/
		usleep(100000); //100 ms sleep
	}
	return 0;
}




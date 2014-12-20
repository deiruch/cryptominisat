#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import ssl
import socket
import sys
import optparse
import struct
import pickle
import threading
import random
import time
import subprocess
import resource
import pprint
import traceback
import boto
pp = pprint.PrettyPrinter(depth=6)

class PlainHelpFormatter(optparse.IndentedHelpFormatter):
    def format_description(self, description):
        if description:
            return description + "\n"
        else:
            return ""

usage = "usage: %prog"
parser = optparse.OptionParser(usage=usage, formatter=PlainHelpFormatter())
parser.add_option("--verbose", "-v", action="store_true"
                    , default=False, dest="verbose"
                    , help="Be more verbose"
                    )

parser.add_option("--host"
                    , dest="host"
                    , help="Host to connect to as a client"
                    )
parser.add_option("--port", "-p"
                    , default=10000, dest="port"
                    , help="Port to use", type="int"
                    )

parser.add_option("--temp"
                    , default="/tmp/", dest="temp_space"
                    , help="Temporary space to use", type=str
                    )

parser.add_option("--test"
                    , default=False, dest="test", action="store_true"
                    , help="only one CNF"
                    )

(options, args) = parser.parse_args()
if options.test:
    options.host = "localhost"
    options.temp_space = "."

if not options.host:
    parser.error('Host not given')


exitapp = False

class solverThread (threading.Thread):
    def __init__(self, threadID):
        threading.Thread.__init__(self)
        self.threadID = threadID

    def setlimits(self):
        #sys.stdout.write("Setting resource limit in child (pid %d). Time %d s Mem %d MB\n" % (os.getpid(), self.indata["timeout_in_secs"], self.indata["mem_limit_in_mb"]))
        resource.setrlimit(resource.RLIMIT_CPU, (self.indata["timeout_in_secs"], self.indata["timeout_in_secs"]))
        resource.setrlimit(resource.RLIMIT_DATA, (self.indata["mem_limit_in_mb"]*1024*1024, self.indata["mem_limit_in_mb"]*1024*1024))

    def get_n_bytes_from_connection(self, connection, n) :
        got = 0
        fulldata = ""
        while got < n :
            data = connection.recv(n-got)
            #print >>sys.stderr, 'received "%s"' % data
            if data :
                fulldata += data
                got += len(data)
            else :
                print >>sys.stderr, "no more data ooops!"
                raise Exception("wanted more data...")

        return fulldata

    def connect_client(self) :
        # Create a socket object
        sock = socket.socket()

        # Get local machine name
        if options.host == None :
            print "You must supply the host to connect to as a client"
            exit(-1)

        print "hostname:", options.host
        host = socket.gethostbyname_ex(options.host)
        print time.strftime("%c"), "Connecting to host" , host
        sock.connect((host[2][0], options.port))

        return sock

    def get_output_fname(self):
        return "%s/%s" % ( \
            options.temp_space, \
            self.indata["cnf_filename"]
        )

    def solver_fname(self):
        return "%s" % (self.indata["solver"])

    def get_stdout_fname(self):
        return self.get_output_fname()+".stdout"

    def get_stderr_fname(self):
        return self.get_output_fname()+".stderr"

    def get_toexec(self):
        toexec = "%s %s/%s"  % \
            (self.indata["solver"], \
            self.indata["cnf_dir"], \
            self.indata["cnf_filename"]
        )

        return toexec

    def execute(self) :
        toexec = self.get_toexec()
        stdout_file = open(self.get_stdout_fname(), "w+")
        stderr_file = open(self.get_stderr_fname(), "w+")

        #limit time
        limits_printed = "Thread %d executing '%s' with timeout %d s  and memout %d MB" % (\
            self.threadID, \
            toexec, \
            self.indata["timeout_in_secs"], \
            self.indata["mem_limit_in_mb"] \
        )
        print limits_printed
        stderr_file.write(limits_printed + "\n")
        stderr_file.flush()
        stdout_file.write(limits_printed + "\n")
        stdout_file.flush()

        tstart = time.time()
        p = subprocess.Popen(toexec.rsplit(), stderr=stderr_file, stdout=stdout_file, preexec_fn=self.setlimits)
        p.wait()

        stderr_file.write("return code: %d\n" % p.returncode)
        stdout_file.write("return code: %d\n" % p.returncode)
        stderr_file.close()
        stdout_file.close()

        tend = time.time()
        print "Finished in %f seconds by thread %s" % (tend-tstart, self.threadID)
        print "return code: ", p.returncode
        return p.returncode, toexec

    def create_url(self, bucket, folder, key):
        return 'https://%s.s3.amazonaws.com/%s/%s' % (bucket, folder, key)

    def copy_solution_to_s3(self) :
        os.system("gzip %s" % self.get_stdout_fname())
        boto_bucket = boto_conn.get_bucket(self.indata["s3_bucket"])
        k = boto.s3.key.Key(boto_bucket)

        fname_with_stdout_ending = self.indata["cnf_filename"]+".stdout.gz"
        k.key = self.indata["s3_folder"]+"/"+ fname_with_stdout_ending
        boto_bucket.delete_key(k)
        k.set_contents_from_filename(self.get_stdout_fname()+".gz")
        url = self.create_url(self.indata["s3_bucket"], self.indata["s3_folder"], fname_with_stdout_ending)
        print "URL: ", url

        os.system("gzip %s" % self.get_stderr_fname())
        fname_with_stderr_ending = self.indata["cnf_filename"]+".stderr.gz"
        k.key = self.indata["s3_folder"]+"/"+fname_with_stderr_ending
        boto_bucket.delete_key(k)
        k.set_contents_from_filename(self.get_stderr_fname()+".gz")
        url = self.create_url(self.indata["s3_bucket"], self.indata["s3_folder"], fname_with_stderr_ending)
        print "URL: ", url

        print "Uploaded stdout+stderr files"

        #k.make_public()
        #print "File public"

        #os.unlink(self.get_stdout_fname())
        #os.unlink(self.get_stderr_fname())

    def uptime(self):
        with open('/proc/uptime', 'r') as f:
            return float(f.readline().split()[0])

        return None

    def ask_for_data_to_solve(self, sock):
        print "asking for stuff to solve..."
        tosend = {}
        tosend["uptime"] = self.uptime()
        tosend["command"] = "need"
        tosend = pickle.dumps(tosend)
        tosend = struct.pack('q', len(tosend)) + tosend
        sock.sendall(tosend)

        #get stuff to solve
        data = self.get_n_bytes_from_connection(sock, 8)
        length = struct.unpack('q', data)[0]
        data = self.get_n_bytes_from_connection(sock, length)
        self.indata = pickle.loads(data)

    def run_loop(self):
        while not exitapp :
            try:
                sock = self.connect_client()
            except Exception as inst:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                traceback.print_exc()
                print "Problem, waiting and re-connecting"
                time.sleep(3)
                continue

            self.ask_for_data_to_solve(sock)
            sock.close()

            print "Got data from server ", pp.pprint(self.indata)
            if self.indata["command"] == "finish":
                print "Client received that there is nothing more to solve, exiting this thread"
                return

            assert self.indata["command"] == "solve"
            returncode, executed = self.execute()
            self.copy_solution_to_s3()

            sock = self.connect_client()

            tosend = {}
            tosend["command"]  = "done"
            tosend["file_num"]  = self.indata["file_num"]
            with open(self.get_stderr_fname(), 'r') as content_file:
                content_stderr = content_file.read()

            tosend["stderr"] = content_stderr
            tosend["returncode"] = returncode

            tosend = pickle.dumps(tosend)
            tosend = struct.pack('q', len(tosend)) + tosend
            sock.sendall(tosend)
            print time.strftime("%c"), "Sent that we finished", self.indata["file_num"], "with retcode", returncode
            print "stderr:", content_stderr

    def run(self):
        print "Starting Thread %d" % self.threadID

        try:
            self.run_loop()
        except KeyboardInterrupt:
            exitapp = True
            raise

boto_conn = boto.connect_s3()

def num_cpus() :
    num_cpu=0
    cpuinfo = open("/proc/cpuinfo", "r")
    for line in cpuinfo:
        if "processor" in line:
            num_cpu+=1

    cpuinfo.close()
    return num_cpu

num_threads = num_cpus()
print "Running with %d threads" % num_threads
if options.test:
    num_threads = 1

ret = 0
#ret = os.system('/home/ubuntu/build.sh')

if ret == 0 :
    threads = []
    for i in range(num_threads) :
        threads.append(solverThread(i))

    for t in threads:
        t.start()

    for t in threads:
        t.join()

print "Exiting Main Thread, shutting down"
toexec = "sudo shutdown -h now"
#print os.system(toexec)

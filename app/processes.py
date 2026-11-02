"""Own a subprocess lifetime, including descendants after the root has exited.

Windows: suspend -> assign to a kill-on-close Job -> resume. Assignment failure
fails launch; it never falls back to uncontained execution. POSIX: own session.
This is lifecycle ownership, not a security sandbox.
"""
import os
import signal
import subprocess
import threading
import time


class WindowsJob:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c = c
        self.api = c.WinDLL('kernel32', use_last_error=True)
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64),
                ('flags', w.DWORD), ('min_working', c.c_size_t), ('max_working', c.c_size_t),
                ('active_limit', w.DWORD), ('affinity', c.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(n, c.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        class Accounting(c.Structure):
            _fields_ = [(n,c.c_int64) for n in ('user','kernel','period_user','period_kernel')] + [
                (n,w.DWORD) for n in ('faults','total','active','terminated')]
        self.Accounting = Accounting
        signatures = {
            'CreateJobObjectW': ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE,w.HANDLE],w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p],w.BOOL),
            'TerminateJobObject': ([w.HANDLE,w.UINT],w.BOOL),
            'CloseHandle': ([w.HANDLE],w.BOOL),
        }
        for name,(args,returns) in signatures.items():
            fn = getattr(self.api,name); fn.argtypes = args; fn.restype = returns
        self.handle = self.api.CreateJobObjectW(None,None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway
        if not self.api.SetInformationJobObject(self.handle,9,c.byref(limits),c.sizeof(limits)):
            error = c.WinError(c.get_last_error()); self.close(); raise error

    def assign(self, proc):
        if not self.api.AssignProcessToJobObject(self.handle,int(proc._handle)):
            raise self.c.WinError(self.c.get_last_error())

    def stop(self):
        if not self.handle:
            return
        if not self.api.TerminateJobObject(self.handle,1):
            raise self.c.WinError(self.c.get_last_error())
        deadline = time.monotonic() + 5
        while True:
            data = self.Accounting()
            if not self.api.QueryInformationJobObject(self.handle,1,self.c.byref(data),self.c.sizeof(data),None):
                raise self.c.WinError(self.c.get_last_error())
            if data.active == 0:
                return
            if time.monotonic() > deadline:
                raise OSError('Owned CLI job still has active processes after termination')
            time.sleep(.02)

    def close(self):
        if self.handle:
            handle,self.handle = self.handle,None
            if not self.api.CloseHandle(handle):
                raise self.c.WinError(self.c.get_last_error())


class ProcessScope:
    def __init__(self):
        self.proc = None
        self.job = WindowsJob() if os.name == 'nt' else None
        self.lock = threading.Lock()

    def spawn(self, command, **kwargs):
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 4} if self.job else {'start_new_session': True}
        self.proc = subprocess.Popen(command, **kwargs, **flags)
        if self.job:
            try:
                self.job.assign(self.proc)
                import psutil
                psutil.Process(self.proc.pid).resume()
            except BaseException:
                # Popen owns the process handle, so this cannot kill a reused PID.
                self.proc.kill(); self.proc.wait()
                for pipe in (self.proc.stdout, self.proc.stderr):
                    if pipe: pipe.close()
                raise
        return self.proc

    def stop(self):
        with self.lock:
            if self.job:
                self.job.stop()
            elif self.proc:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def close(self):
        with self.lock:
            if self.job:
                self.job.close()

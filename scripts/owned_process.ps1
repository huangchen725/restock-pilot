# Suspended creation avoids a race in which an installer child escapes ownership.
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public sealed class RestockOwnedProcess : IDisposable {
  [StructLayout(LayoutKind.Sequential)] struct Basic { public long pt,jt; public uint flags; public UIntPtr min,max; public uint active; public UIntPtr affinity; public uint priority,scheduling; }
  [StructLayout(LayoutKind.Sequential)] struct Extended { public Basic basic; public ulong a,b,c,d,e,f; public UIntPtr pm,jm,pp,pj; }
  [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Startup { public uint cb; public string reserved,desktop,title; public uint x,y,xs,ys,xc,yc,fill,flags; public ushort show,reserved2; public IntPtr reserved3,input,output,error; }
  [StructLayout(LayoutKind.Sequential)] struct PI { public IntPtr process,thread; public uint pid,tid; }
  [StructLayout(LayoutKind.Sequential)] struct SA { public int length; public IntPtr descriptor; public int inherit; }
  [DllImport("kernel32",SetLastError=true,CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a,string b);
  [DllImport("kernel32",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr h,int c,ref Extended e,int size);
  [DllImport("kernel32",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
  [DllImport("kernel32",SetLastError=true,CharSet=CharSet.Unicode)] static extern bool CreateProcess(string app,StringBuilder cmd,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref Startup s,out PI pi);
  [DllImport("kernel32",SetLastError=true,CharSet=CharSet.Unicode)] static extern IntPtr CreateFile(string name,uint access,uint share,ref SA sa,uint creation,uint flags,IntPtr template);
  [DllImport("kernel32")] static extern uint ResumeThread(IntPtr h);
  [DllImport("kernel32")] static extern uint WaitForSingleObject(IntPtr h,uint timeout);
  [DllImport("kernel32")] static extern bool GetExitCodeProcess(IntPtr h,out uint code);
  [DllImport("kernel32")] static extern bool TerminateProcess(IntPtr h,uint code);
  [DllImport("kernel32")] static extern bool CloseHandle(IntPtr h);
  IntPtr job,process;
  public RestockOwnedProcess(string executable,string arguments,string cwd,string log) {
    job=CreateJobObject(IntPtr.Zero,null); Extended limits=new Extended(); limits.basic.flags=0x2000;
    if(job==IntPtr.Zero || !SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(limits))) throw new Exception("Cannot protect installer process lifetime");
    SA sa=new SA(); sa.length=Marshal.SizeOf(sa); sa.inherit=1;
    IntPtr output=CreateFile(log,0x40000000,3,ref sa,2,0x80,IntPtr.Zero);
    IntPtr input=CreateFile("NUL",0x80000000,3,ref sa,3,0x80,IntPtr.Zero);
    PI pi; Startup s=new Startup(); s.cb=(uint)Marshal.SizeOf(s); s.flags=0x100; s.output=output; s.error=output; s.input=input;
    bool ok=CreateProcess(executable,new StringBuilder("\""+executable+"\" "+arguments),IntPtr.Zero,IntPtr.Zero,true,0x08000004,IntPtr.Zero,cwd,ref s,out pi);
    CloseHandle(output); CloseHandle(input);
    if(!ok) { Dispose(); throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()); }
    process=pi.process;
    if(!AssignProcessToJobObject(job,process)) { TerminateProcess(process,1); CloseHandle(pi.thread); Dispose(); throw new Exception("Cannot own installer process"); }
    if(ResumeThread(pi.thread)==0xffffffff) { CloseHandle(pi.thread); Dispose(); throw new Exception("Cannot resume installer process"); }
    CloseHandle(pi.thread);
  }
  public bool Done { get { return WaitForSingleObject(process,0)==0; } }
  public int ExitCode { get { uint code; GetExitCodeProcess(process,out code); return (int)code; } }
  public void Dispose() { if(job!=IntPtr.Zero) { CloseHandle(job); job=IntPtr.Zero; } if(process!=IntPtr.Zero) { CloseHandle(process); process=IntPtr.Zero; } }
}
'@

"""Ordinary-user launcher. No terminal or personal Codex configuration required."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox,ttk
import webbrowser

ROOT=Path(__file__).resolve().parent


def read_settings():
    from dotenv import dotenv_values
    return {**dotenv_values(ROOT/'.env',interpolate=False,encoding='utf-8-sig'),**os.environ}


def save_settings(mode,key):
    from dotenv import set_key
    if any(c in key for c in '\r\n\0'): raise ValueError('密钥不能包含换行。')
    path=ROOT/'.env'
    if not path.exists(): path.write_bytes((ROOT/'.env.example').read_bytes())
    set_key(path,'RESTOCK_AGENT_MODE',mode,quote_mode='always',encoding='utf-8')
    if key.strip(): set_key(path,'INTERN_API_KEY',key.strip(),quote_mode='always',encoding='utf-8')


def main():
    from launcher_control import request_control,read_instance
    window=tk.Tk(); window.title('RestockPilot 启动窗口'); window.geometry('700x540'); window.resizable(False,False)
    style=ttk.Style(); style.theme_use('clam')
    style.configure('.',font=('Microsoft YaHei UI',10))
    box=ttk.Frame(window,padding=24); box.pack(fill='both',expand=True)
    ttk.Label(box,text='RestockPilot',font=('Microsoft YaHei UI',23,'bold')).pack(anchor='w')
    ttk.Label(box,text='安装环境、打开补货工作台，以及安全停止本次服务。').pack(anchor='w',pady=(3,18))
    settings=read_settings()
    mode=tk.StringVar(value='离线演示' if settings.get('RESTOCK_AGENT_MODE')=='off' else '真实模型')
    ttk.Label(box,text='运行模式').pack(anchor='w')
    combo=ttk.Combobox(box,textvariable=mode,values=['真实模型','离线演示'],state='readonly',width=20)
    combo.pack(anchor='w',pady=5)
    ttk.Label(box,text='模型密钥（留空保留已配置值；离线演示不需要）').pack(anchor='w',pady=(8,3))
    key=tk.StringVar(); field=ttk.Entry(box,textvariable=key,show='●',width=66); field.pack(anchor='w')
    configured=bool(settings.get('INTERN_API_KEY')) and settings.get('INTERN_API_KEY')!='在这里填端砚key'
    note=tk.StringVar(value='已配置模型密钥。' if configured else '尚未配置模型密钥；可先使用离线演示。')
    ttk.Label(box,textvariable=note).pack(anchor='w',pady=5)
    status=tk.StringVar(value='尚未启动。关闭网页标签不会停止服务。')
    ttk.Label(box,textvariable=status,wraplength=620).pack(anchor='w',pady=(15,12))
    actions=ttk.Frame(box); actions.pack(fill='x')
    messages=queue.Queue(); process=None; owner=None; log_file=None; busy=False; closing=False; opened=False

    def run_async(work):
        redaction_env={**read_settings(),'NEW_KEY':key.get()}
        def wrapped():
            try: messages.put(('ok',work()))
            except Exception as exc:
                from start import redact
                messages.put(('error',redact(str(exc),redaction_env)))
        threading.Thread(target=wrapped,daemon=True).start()

    def save():
        save_settings('off' if mode.get()=='离线演示' else 'required',key.get())
        note.set('设置已保存；运行中的服务需停止后重新启动。')

    def launch():
        nonlocal process,owner,log_file,busy,opened
        if busy: return
        existing=read_instance()
        if existing:
            expected='off' if mode.get()=='离线演示' else 'required'
            if existing.get('mode')!=expected:
                messagebox.showinfo('先停止再切换模式','已有服务正在使用另一种模式，请先停止服务后重新启动。',parent=window); return
            info=request_control()
            if info.get('state')!='ready':
                status.set('已有启动窗口正在准备服务，请等待其显示就绪。'); return
            webbrowser.open('http://127.0.0.1:8501'); status.set('服务已启动，已打开现有工作台。'); return
        try:
            save()
            from app.processes import ProcessScope
            env=read_settings()
            env.update(RESTOCK_AGENT_MODE='off' if mode.get()=='离线演示' else 'required',PYTHONIOENCODING='utf-8',PYTHONUTF8='1')
            if key.get().strip(): env['INTERN_API_KEY']=key.get().strip()
            env={k:str(v) for k,v in env.items() if v is not None}
            path=ROOT/'outputs/launcher/desktop.log'; path.parent.mkdir(parents=True,exist_ok=True)
            log_file=path.open('w',encoding='utf-8')
            owner=ProcessScope()
            python=Path(sys.executable).with_name('python.exe') if os.name=='nt' else Path(sys.executable)
            # Open the user's browser from this desktop process, outside the
            # service job; stopping services must not terminate their browser.
            process=owner.spawn([str(python),str(ROOT/'start.py')],cwd=ROOT,env=env,
                                stdin=subprocess.DEVNULL,stdout=log_file,stderr=subprocess.STDOUT)
            busy=True; opened=False; status.set('正在检查安装并启动服务，请稍候…')
        except Exception as exc: messagebox.showerror('无法启动',str(exc),parent=window)

    def stop(confirm=True):
        nonlocal busy,closing
        if read_instance() and confirm and not messagebox.askokcancel('停止并退出',
                '将停止本次所有服务。进行中的处理会中断；已创建申请会保留，下次先核对再恢复。',parent=window): return
        closing=True; busy=True; status.set('正在停止服务并释放后台进程…')
        def work():
            request_control('stop')
            deadline=time.monotonic()+20
            while read_instance() and time.monotonic()<deadline: time.sleep(.2)
            if read_instance(): raise RuntimeError('停止尚未完成，请稍候再试。')
            return '服务已停止，后台进程已释放。'
        run_async(work)

    def install():
        if busy or read_instance(): messagebox.showinfo('请先停止','请先停止服务，再安装或修复环境。',parent=window); return
        powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        subprocess.Popen([str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/install_window.ps1')],
                          cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
        window.destroy()

    def reset_connections():
        if read_instance(): messagebox.showinfo('请先停止','停止服务后再恢复本地连接设置。',parent=window); return
        from dotenv import set_key
        save_settings('off' if mode.get()=='离线演示' else 'required','')
        for name,value in {'RESTOCK_API_URL':'http://127.0.0.1:8100',
                           'RESTOCK_LITELLM_URL':'http://127.0.0.1:4000',
                           'PROCUREMENT_PORTAL_URL':'http://127.0.0.1:8200'}.items():
            set_key(ROOT/'.env',name,value,quote_mode='always',encoding='utf-8')
            # An explicit UI reset applies to this launcher session as well.
            os.environ[name]=value
        if (ROOT/'.venv/codex/node_modules/.bin/codex.cmd').exists():
            set_key(ROOT/'.env','RESTOCK_CODEX_BIN','.venv/codex/node_modules/.bin/codex.cmd',quote_mode='always',encoding='utf-8')
            os.environ['RESTOCK_CODEX_BIN']=str(ROOT/'.venv/codex/node_modules/.bin/codex.cmd')
        status.set('已恢复本项目的默认本地连接；模型密钥与业务资料未修改。')

    def save_clicked():
        try: save()
        except Exception: messagebox.showerror('设置未保存','请检查密钥输入和项目配置文件写权限；现有运行不会因此切换。',parent=window)

    ttk.Button(actions,text='保存设置',command=save_clicked).pack(side='left',padx=(0,8))
    ttk.Button(actions,text='启动 / 打开工作台',command=launch).pack(side='left',padx=8)
    ttk.Button(actions,text='停止并退出',command=stop).pack(side='left',padx=8)
    footer=ttk.Frame(box); footer.pack(fill='x',pady=18)
    ttk.Button(footer,text='安装 / 修复环境',command=install).pack(side='left')
    ttk.Button(footer,text='恢复本地连接设置',command=reset_connections).pack(side='left',padx=12)
    ttk.Label(box,text='服务只在本机运行。申请创建不表示付款或到货。\n关闭此窗口会停止服务；已经保存的资料与任务会保留。',wraplength=620).pack(anchor='w')

    def poll():
        nonlocal busy,closing,owner,log_file,process,opened
        try:
            kind,text=messages.get_nowait()
            status.set(text)
            if kind=='error': closing=False; busy=False; messagebox.showerror('操作未完成',text,parent=window)
            elif closing:
                if owner: owner.close()
                if log_file: log_file.close()
                window.destroy(); return
        except queue.Empty: pass
        if process and process.poll() is not None:
            code=process.returncode
            if owner: owner.close(); owner=None
            if log_file: log_file.close(); log_file=None
            process=None; busy=False
            if closing: window.destroy(); return
            if code:
                path=ROOT/'outputs/launcher/desktop.log'
                details=path.read_text(encoding='utf-8',errors='replace')[-1600:]
                status.set('启动未完成，已清理本次后台进程。')
                messagebox.showerror('启动未完成',details,parent=window)
            else:
                # The workbench Stop button also closes the desktop owner.
                window.destroy(); return
        elif process and not closing:
            try:
                info=request_control()
                if info.get('state')=='ready':
                    status.set('工作台已就绪。关闭网页可继续后台处理；停止请点“停止并退出”。'); busy=False
                    if not opened:
                        webbrowser.open('http://127.0.0.1:8501'); opened=True
            except Exception: pass
        window.after(700,poll)
    window.protocol('WM_DELETE_WINDOW',stop)
    if '--stop' in sys.argv: window.after(150,lambda:stop(False))
    window.after(400,poll)
    try: window.mainloop()
    finally:
        if owner: owner.close()
        if log_file: log_file.close()


if __name__=='__main__':
    try: main()
    except Exception:
        messagebox.showerror('环境尚未就绪','请双击“安装或修复 RestockPilot”完成环境安装后重试。')

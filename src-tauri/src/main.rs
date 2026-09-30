#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewUrl};

const PORT: u16 = 8642;
const APP_URL: &str = "http://127.0.0.1:8642";

/// 后端子进程句柄（退出时树杀）
struct Backend(Mutex<Option<Child>>);

fn backend_exe(resource_dir: &PathBuf) -> PathBuf {
    resource_dir.join("backend").join("media-squirrel-backend.exe")
}

fn spawn_backend(resource_dir: &PathBuf) -> Option<Child> {
    let exe = backend_exe(resource_dir);
    if !exe.exists() {
        eprintln!("[tauri] 后端 exe 不存在: {}", exe.display());
        return None;
    }
    Command::new(&exe)
        .args([
            "--no-tray",
            "--no-browser",
            "--port",
            &PORT.to_string(),
        ])
        .spawn()
        .ok()
}

fn wait_port(timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if TcpStream::connect(("127.0.0.1", PORT)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

#[cfg(windows)]
fn kill_tree(pid: u32) {
    use std::os::windows::process::CommandExt;
    // CREATE_NO_WINDOW，避免闪烁黑色窗口
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(0x0800_0000)
        .spawn();
}

#[cfg(not(windows))]
fn kill_tree(pid: u32) {
    let _ = Command::new("kill").args(["-9", &pid.to_string()]).spawn();
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let resource_dir = app.path().resource_dir()?;
            let child = spawn_backend(&resource_dir);
            app.manage(Backend(Mutex::new(child)));

            // 独立线程：等端口就绪 -> 导航到后端 URL -> 显示窗口
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let ready = wait_port(Duration::from_secs(90));
                if let Some(win) = handle.get_webview_window("main") {
                    if ready {
                        if let Ok(url) = APP_URL.parse::<tauri::Url>() {
                            let _ = win.navigate(url);
                        }
                        let _ = win.show();
                        let _ = win.set_focus();
                    } else {
                        // 后端启动失败：显示错误页（eval 注入，不依赖资源文件）
                        let _ = win.eval(
                            "document.body.innerHTML='<div style=\"height:100vh;display:flex;align-items:center;justify-content:center;background:#f5f5f7;font-family:'PingFang SC','Microsoft YaHei',sans-serif\"><div style=\"text-align:center;color:#1d1d1f\"><div style=\"font-size:64px\">🐿️</div><h2>后端服务启动失败</h2><p style=\"color:#6e6e73\">请重启应用；若持续失败，请在任务管理器结束 media-squirrel-backend.exe 后重试</p></div></div>';document.title='Media Squirrel - 启动失败'",
                        );
                        let _ = win.show();
                        eprintln!("[tauri] 后端 90 秒内未就绪");
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<Backend>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.as_ref() {
                            kill_tree(child.id());
                        }
                        // 顺带兜底：直接 kill 句柄（taskkill 已树杀，这里保险）
                        let _ = guard.take();
                    }
                }
            }
        });
}

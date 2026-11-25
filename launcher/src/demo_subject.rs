use std::env;
use std::fs::OpenOptions;
use std::io::Write;
use std::net::TcpStream;
use std::thread::sleep;
use std::time::Duration;

/// 演示主体程序：等待片刻后连回 Launcher 的监听端口发送 READY。
/// 环境变量 `DEMO_SUBJECT_MODE` 可取 `exit`、`never` 或 `bad-token`，用于演示失败路径。
pub fn run() -> Result<(), String> {
    let port = env::var("MINECRAFTTOOLS_LAUNCHER_PORT")
        .map_err(|_| "缺少 MINECRAFTTOOLS_LAUNCHER_PORT".to_string())?
        .parse::<u16>()
        .map_err(|_| "MINECRAFTTOOLS_LAUNCHER_PORT 不是有效端口".to_string())?;
    let token = env::var("MINECRAFTTOOLS_LAUNCHER_TOKEN").unwrap_or_default();
    let mode = match env::var("DEMO_SUBJECT_APPLY_MODE_TO_VERSION") {
        Ok(version) if env::var("MINECRAFTTOOLS_VERSION").ok().as_deref() != Some(&version) => {
            String::new()
        }
        _ => env::var("DEMO_SUBJECT_MODE").unwrap_or_default(),
    };
    log_line(&format!("demo subject 启动，模式：{mode}"));

    if mode == "exit" {
        log_line("演示设置：在报告就绪前退出");
        std::process::exit(1);
    }

    sleep(millis("DEMO_SUBJECT_DELAY_MS", 1200));
    if mode == "never" {
        log_line("演示设置：不发送 READY");
        sleep(Duration::from_secs(60));
        return Ok(());
    }

    let mut stream = TcpStream::connect(("127.0.0.1", port))
        .map_err(|error| format!("无法连接 Launcher：{error}"))?;
    let ready = if mode == "bad-token" {
        format!("READY {token}-invalid\n")
    } else {
        format!("READY {token}\n")
    };
    stream
        .write_all(ready.as_bytes())
        .map_err(|error| format!("无法发送 READY：{error}"))?;
    log_line("已发送 READY");
    sleep(millis("DEMO_SUBJECT_LIFETIME_MS", 8000));
    Ok(())
}

fn millis(name: &str, default: u64) -> Duration {
    let value = env::var(name)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(default);
    Duration::from_millis(value)
}

fn log_line(message: &str) {
    let Some(path) = env::var_os("MINECRAFTTOOLS_CLIENT_LOG") else {
        return;
    };
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{message}");
    }
}

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

use crate::http::HttpClient;

pub const LAUNCHER_VERSION: &str = match option_env!("MINECRAFTTOOLS_VERSION") {
    Some(version) => version,
    None => "0.0.0-local",
};

/// 启动失败时保存并上报的诊断数据。字段只包含定位问题所需的信息。
#[derive(Serialize)]
pub struct Report {
    pub created_at: u64,
    pub launcher_version: String,
    pub os: String,
    pub os_version: String,
    pub arch: String,
    pub channel: String,
    pub build_channel: String,
    pub version: String,
    pub stage: String,
    pub exit_code: Option<i32>,
    pub log: Vec<String>,
}

impl Report {
    pub fn new(stage: &str) -> Self {
        Self {
            created_at: now(),
            launcher_version: LAUNCHER_VERSION.to_string(),
            os: std::env::consts::OS.to_string(),
            os_version: system_version(),
            arch: std::env::consts::ARCH.to_string(),
            channel: "-".into(),
            build_channel: "-".into(),
            version: "-".into(),
            stage: stage.to_string(),
            exit_code: None,
            log: Vec::new(),
        }
    }
}

pub fn save(log_directory: &Path, report: &Report) -> Result<PathBuf, String> {
    fs::create_dir_all(log_directory).map_err(|error| format!("无法创建日志目录：{error}"))?;
    let path = log_directory.join(format!(
        "launcher-failure-{}-{}.json",
        report.created_at,
        std::process::id()
    ));
    let text = serde_json::to_string_pretty(report)
        .map_err(|error| format!("无法序列化诊断报告：{error}"))?;
    fs::write(&path, text + "\n")
        .map_err(|error| format!("无法写入 {}：{error}", path.display()))?;
    Ok(path)
}

pub fn upload(client: &HttpClient, url: &str, report: &Report) -> Result<(), String> {
    let body =
        serde_json::to_string(report).map_err(|error| format!("无法序列化诊断报告：{error}"))?;
    client.post_json(url, &body)
}

/// 保留最近 40 行，并把长十六进制串（令牌、摘要等）替换掉。
pub fn sanitize(lines: &[String]) -> Vec<String> {
    let start = lines.len().saturating_sub(40);
    lines[start..]
        .iter()
        .map(|line| redact_line(line))
        .collect()
}

fn redact_line(line: &str) -> String {
    let mut result = String::with_capacity(line.len());
    let mut hex_run = String::new();
    for character in line.chars() {
        if character.is_ascii_hexdigit() {
            hex_run.push(character);
        } else {
            flush_hex(&mut result, &mut hex_run);
            result.push(character);
        }
    }
    flush_hex(&mut result, &mut hex_run);
    if result.chars().count() > 300 {
        let mut shortened: String = result.chars().take(300).collect();
        shortened.push('…');
        shortened
    } else {
        result
    }
}

fn flush_hex(result: &mut String, hex_run: &mut String) {
    if hex_run.len() >= 32 {
        result.push_str("<已脱敏>");
    } else {
        result.push_str(hex_run);
    }
    hex_run.clear();
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}

fn system_version() -> String {
    if cfg!(target_os = "macos") {
        command_output("sw_vers", &["-productVersion"])
    } else if cfg!(target_os = "windows") {
        command_output("cmd", &["/C", "ver"])
    } else if cfg!(target_os = "linux") {
        fs::read_to_string("/etc/os-release")
            .ok()
            .and_then(|text| {
                text.lines().find_map(|line| {
                    line.strip_prefix("PRETTY_NAME=")
                        .map(|value| value.trim_matches('"').to_string())
                })
            })
            .unwrap_or_else(|| "unknown".into())
    } else {
        "unknown".into()
    }
}

fn command_output(program: &str, arguments: &[&str]) -> String {
    let mut command = std::process::Command::new(program);
    command.args(arguments);
    // 发布版是 GUI 程序，避免在 Windows 上弹出控制台窗口。
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    command
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_long_hex_tokens() {
        let lines = vec!["setup token=0123456789abcdef0123456789abcdef ok".to_string()];
        let sanitized = sanitize(&lines);
        assert_eq!(sanitized[0], "setup token=<已脱敏> ok");
    }

    #[test]
    fn keeps_short_hex_text() {
        let lines = vec!["下载更新 2026-11-25-1".to_string()];
        assert_eq!(sanitize(&lines)[0], "下载更新 2026-11-25-1");
    }

    #[test]
    fn keeps_only_the_last_forty_lines() {
        let lines = (0..60)
            .map(|index| format!("line {index}"))
            .collect::<Vec<_>>();
        let sanitized = sanitize(&lines);
        assert_eq!(sanitized.len(), 40);
        assert_eq!(sanitized[0], "line 20");
    }

    #[test]
    fn report_serializes_expected_fields() {
        let mut report = Report::new("ready-timeout");
        report.channel = "dev".into();
        report.log = vec!["启动主体程序".into()];
        let text = serde_json::to_string(&report).unwrap();
        assert!(text.contains("\"stage\":\"ready-timeout\""));
        assert!(text.contains("\"channel\":\"dev\""));
        assert!(text.contains("\"launcher_version\""));
    }
}

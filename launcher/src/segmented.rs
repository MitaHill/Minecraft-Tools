//! 分段多线程下载原型。
//!
//! ureq 只提供单请求 HTTP 客户端，没有内建的并发分段下载或连接数爬坡。
//! 这里的控制器先探测服务器是否支持 `Range`，再从 1 个连接开始按固定间隔
//! 逐个增加，直到配置的上限或工作队列耗尽。

use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

#[derive(Clone, Debug)]
pub struct Config {
    pub max_connections: usize,
    pub chunk_size: u64,
    pub ramp_interval: Duration,
    pub request_timeout: Duration,
    pub max_retries: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            max_connections: 8,
            chunk_size: 4 * 1024 * 1024,
            ramp_interval: Duration::from_millis(750),
            request_timeout: Duration::from_secs(60),
            max_retries: 3,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Summary {
    pub bytes: u64,
    pub sha256: String,
    pub fallback: bool,
    pub connections_peak: usize,
    pub ramp: Vec<(Duration, usize)>,
}

struct Probe {
    total: u64,
    ranged: bool,
    validator: Option<String>,
}

/// 下载到 `destination`。`progress` 接收（已下载字节，总字节；未知时为 0）。
pub fn download(
    url: &str,
    destination: &Path,
    config: &Config,
    progress: impl Fn(u64, u64) + Send + Sync + 'static,
) -> Result<Summary, String> {
    let progress = Arc::new(progress);
    let probe = probe(url, config)?;
    if !probe.ranged || probe.total == 0 {
        return single(url, destination, config, progress);
    }
    ranged(url, destination, config, probe, progress)
}

fn probe(url: &str, config: &Config) -> Result<Probe, String> {
    let agent = build_agent(config.request_timeout);
    let mut response = agent
        .get(url)
        .header("Range", "bytes=0-0")
        .header("Accept-Encoding", "identity")
        .call()
        .map_err(|error| format!("探测分段下载失败：{error}"))?;
    let status = response.status().as_u16();
    match status {
        206 => {
            let content_range = header(&response, "content-range")
                .ok_or_else(|| "服务器返回 206 但缺少 Content-Range".to_string())?;
            let total = parse_total(&content_range)
                .ok_or_else(|| format!("无法解析 Content-Range：{content_range}"))?;
            let validator =
                header(&response, "etag").or_else(|| header(&response, "last-modified"));
            let _ = response.body_mut().read_to_string();
            Ok(Probe {
                total,
                ranged: true,
                validator,
            })
        }
        200 => {
            let total = header(&response, "content-length")
                .and_then(|value| value.parse().ok())
                .unwrap_or(0);
            Ok(Probe {
                total,
                ranged: false,
                validator: None,
            })
        }
        _ => Err(format!("探测分段下载失败：HTTP {status}")),
    }
}

fn single(
    url: &str,
    destination: &Path,
    config: &Config,
    progress: Arc<dyn Fn(u64, u64) + Send + Sync>,
) -> Result<Summary, String> {
    let agent = build_agent(config.request_timeout);
    let mut response = agent
        .get(url)
        .header("Accept-Encoding", "identity")
        .call()
        .map_err(|error| format!("下载失败：{error}"))?;
    let status = response.status().as_u16();
    if !(200..300).contains(&status) {
        return Err(format!("下载地址返回 HTTP {status}"));
    }
    let total = header(&response, "content-length")
        .and_then(|value| value.parse().ok())
        .unwrap_or(0);
    let mut file = File::create(destination)
        .map_err(|error| format!("无法创建 {}：{error}", destination.display()))?;
    let mut reader = response.body_mut().as_reader();
    let mut hasher = Sha256::new();
    let mut downloaded = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|error| format!("读取响应失败：{error}"))?;
        if read == 0 {
            break;
        }
        file.write_all(&buffer[..read])
            .map_err(|error| format!("写入文件失败：{error}"))?;
        hasher.update(&buffer[..read]);
        downloaded += read as u64;
        progress(downloaded, total);
    }
    file.sync_all()
        .map_err(|error| format!("写入文件失败：{error}"))?;
    Ok(Summary {
        bytes: downloaded,
        sha256: to_hex(&hasher.finalize()),
        fallback: true,
        connections_peak: 1,
        ramp: Vec::new(),
    })
}

fn ranged(
    url: &str,
    destination: &Path,
    config: &Config,
    probe: Probe,
    progress: Arc<dyn Fn(u64, u64) + Send + Sync>,
) -> Result<Summary, String> {
    let total = probe.total;
    let chunk_size = config.chunk_size.max(1);
    let chunks = total.div_ceil(chunk_size);
    let wanted = (config.max_connections.max(1) as u64).min(chunks) as usize;
    let file = File::create(destination)
        .map_err(|error| format!("无法创建 {}：{error}", destination.display()))?;
    file.set_len(total)
        .map_err(|error| format!("无法预分配文件：{error}"))?;
    let file = Arc::new(file);
    let next = Arc::new(AtomicU64::new(0));
    let downloaded = Arc::new(AtomicU64::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    let failure = Arc::new(Mutex::new(None::<String>));
    let active = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicUsize::new(0));
    let ramp = Arc::new(Mutex::new(Vec::<(Duration, usize)>::new()));
    let started = Instant::now();
    let url = url.to_string();
    let validator = probe.validator;
    let worker = {
        let file = Arc::clone(&file);
        let next = Arc::clone(&next);
        let downloaded = Arc::clone(&downloaded);
        let stop = Arc::clone(&stop);
        let failure = Arc::clone(&failure);
        let active = Arc::clone(&active);
        let peak = Arc::clone(&peak);
        let progress = Arc::clone(&progress);
        let url = url.clone();
        let validator = validator.clone();
        let request_timeout = config.request_timeout;
        let max_retries = config.max_retries;
        move || {
            let current = active.fetch_add(1, Ordering::SeqCst) + 1;
            peak.fetch_max(current, Ordering::SeqCst);
            let agent = build_agent(request_timeout);
            loop {
                if stop.load(Ordering::SeqCst) {
                    break;
                }
                let index = next.fetch_add(1, Ordering::SeqCst);
                if index >= chunks {
                    break;
                }
                let start = index * chunk_size;
                let end = (start + chunk_size).min(total) - 1;
                let mut attempt = 0;
                loop {
                    match fetch_range(
                        &agent,
                        &url,
                        start,
                        end,
                        validator.as_deref(),
                        &file,
                        &downloaded,
                        &progress,
                        total,
                    ) {
                        Ok(()) => break,
                        Err(_error) if attempt < max_retries => {
                            attempt += 1;
                            thread::sleep(Duration::from_millis(200 * (1 << attempt)));
                        }
                        Err(error) => {
                            let mut slot = failure.lock().expect("failure mutex");
                            if slot.is_none() {
                                *slot = Some(format!("分段 {start}-{end} 失败：{error}"));
                            }
                            stop.store(true, Ordering::SeqCst);
                            break;
                        }
                    }
                }
            }
            active.fetch_sub(1, Ordering::SeqCst);
        }
    };

    let mut handles = Vec::new();
    handles.push(thread::spawn(worker.clone()));
    ramp.lock()
        .expect("ramp mutex")
        .push((started.elapsed(), 1));
    while next.load(Ordering::SeqCst) < chunks && !stop.load(Ordering::SeqCst) {
        thread::sleep(config.ramp_interval);
        if stop.load(Ordering::SeqCst) || next.load(Ordering::SeqCst) >= chunks {
            break;
        }
        if handles.len() < wanted {
            handles.push(thread::spawn(worker.clone()));
            ramp.lock()
                .expect("ramp mutex")
                .push((started.elapsed(), handles.len()));
        }
    }
    for handle in handles {
        handle.join().map_err(|_| "下载线程异常退出".to_string())?;
    }
    if let Some(error) = failure.lock().expect("failure mutex").take() {
        return Err(error);
    }
    if downloaded.load(Ordering::SeqCst) != total {
        return Err(format!(
            "下载不完整：{} / {total} 字节",
            downloaded.load(Ordering::SeqCst)
        ));
    }
    file.sync_all()
        .map_err(|error| format!("写入文件失败：{error}"))?;
    drop(file);

    let mut reader =
        File::open(destination).map_err(|error| format!("无法读取下载结果：{error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|error| format!("读取下载结果失败：{error}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(Summary {
        bytes: total,
        sha256: to_hex(&hasher.finalize()),
        fallback: false,
        connections_peak: peak.load(Ordering::SeqCst),
        ramp: ramp.lock().expect("ramp mutex").clone(),
    })
}

#[allow(clippy::too_many_arguments)]
fn fetch_range(
    agent: &ureq::Agent,
    url: &str,
    start: u64,
    end: u64,
    validator: Option<&str>,
    file: &File,
    downloaded: &AtomicU64,
    progress: &Arc<dyn Fn(u64, u64) + Send + Sync>,
    total: u64,
) -> Result<(), String> {
    let range = format!("bytes={start}-{end}");
    let mut request = agent
        .get(url)
        .header("Range", range.as_str())
        .header("Accept-Encoding", "identity");
    if let Some(validator) = validator {
        request = request.header("If-Range", validator);
    }
    let mut response = request
        .call()
        .map_err(|error| format!("请求失败：{error}"))?;
    let status = response.status().as_u16();
    if status != 206 {
        return Err(format!("期望 206，实际 HTTP {status}"));
    }
    let content_range =
        header(&response, "content-range").ok_or_else(|| "响应缺少 Content-Range".to_string())?;
    let (response_start, response_end, response_total) = parse_content_range(&content_range)
        .ok_or_else(|| format!("Content-Range 格式错误：{content_range}"))?;
    if (response_start, response_end, response_total) != (start, end, total) {
        return Err(format!(
            "Content-Range 不符：期望 {start}-{end}/{total}，实际 {content_range}"
        ));
    }
    let expected = (end - start + 1) as usize;
    let mut buffer = vec![0u8; expected];
    response
        .body_mut()
        .as_reader()
        .read_exact(&mut buffer)
        .map_err(|error| format!("读取分段失败：{error}"))?;
    write_at(file, &buffer, start).map_err(|error| format!("写入分段失败：{error}"))?;
    let downloaded = downloaded.fetch_add(expected as u64, Ordering::SeqCst) + expected as u64;
    progress(downloaded, total);
    Ok(())
}

fn build_agent(timeout: Duration) -> ureq::Agent {
    let config = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .http_status_as_error(false)
        .build();
    ureq::Agent::new_with_config(config)
}

fn header(response: &ureq::http::Response<ureq::Body>, name: &str) -> Option<String> {
    response
        .headers()
        .get(name)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string)
}

fn parse_total(content_range: &str) -> Option<u64> {
    content_range.rsplit_once('/')?.1.parse().ok()
}

fn parse_content_range(value: &str) -> Option<(u64, u64, u64)> {
    let (unit, rest) = value.split_once(' ')?;
    if unit != "bytes" {
        return None;
    }
    let (range, total) = rest.split_once('/')?;
    let (start, end) = range.split_once('-')?;
    Some((start.parse().ok()?, end.parse().ok()?, total.parse().ok()?))
}

fn write_at(file: &File, data: &[u8], offset: u64) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileExt;
        file.write_all_at(data, offset)
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::FileExt;
        let mut written = 0;
        while written < data.len() {
            let count = file.seek_write(&data[written..], offset + written as u64)?;
            if count == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::WriteZero,
                    "写入文件失败",
                ));
            }
            written += count;
        }
        Ok(())
    }
}

fn to_hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        text.push(DIGITS[(byte >> 4) as usize] as char);
        text.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_content_range() {
        assert_eq!(
            parse_content_range("bytes 0-1023/4096"),
            Some((0, 1023, 4096))
        );
        assert_eq!(parse_total("bytes 0-0/4096"), Some(4096));
        assert_eq!(parse_content_range("bytes */4096"), None);
    }
}

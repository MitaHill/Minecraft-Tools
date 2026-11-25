use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;
use std::time::Duration;

use sha2::{Digest, Sha256};

/// 带固定超时的 HTTP 客户端。健康检查、元数据和更新包共用。
pub struct HttpClient {
    agent: ureq::Agent,
}

impl HttpClient {
    pub fn new(timeout: Duration) -> Self {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(timeout))
            .http_status_as_error(false)
            .build();
        Self {
            agent: ureq::Agent::new_with_config(config),
        }
    }

    pub fn get_status(&self, url: &str) -> Result<u16, String> {
        let mut response = self
            .agent
            .get(url)
            .config()
            .https_only(url.starts_with("https://"))
            .build()
            .call()
            .map_err(|error| format!("请求 {url} 失败：{error}"))?;
        let status = response.status().as_u16();
        // 读完响应体，连接才会被正确回收，避免下一次请求复用已关闭的连接。
        let _ = response.body_mut().read_to_string();
        Ok(status)
    }

    pub fn get_text(&self, url: &str) -> Result<String, String> {
        self.get_text_bounded(url, 1024 * 1024)
    }

    pub fn get_text_bounded(&self, url: &str, limit: u64) -> Result<String, String> {
        let mut response = self
            .agent
            .get(url)
            .config()
            .https_only(url.starts_with("https://"))
            .build()
            .call()
            .map_err(|error| format!("请求 {url} 失败：{error}"))?;
        let status = response.status().as_u16();
        if !(200..300).contains(&status) {
            return Err(format!("服务器返回 HTTP {status}"));
        }
        let mut bytes = Vec::new();
        response
            .body_mut()
            .as_reader()
            .take(limit + 1)
            .read_to_end(&mut bytes)
            .map_err(|error| format!("读取响应失败：{error}"))?;
        if bytes.len() as u64 > limit {
            return Err("响应超过大小限制".into());
        }
        String::from_utf8(bytes).map_err(|_| "响应不是 UTF-8 文本".into())
    }

    pub fn post_json(&self, url: &str, body: &str) -> Result<(), String> {
        let mut response = self
            .agent
            .post(url)
            .config()
            .https_only(url.starts_with("https://"))
            .build()
            .header("content-type", "application/json")
            .send(body.as_bytes())
            .map_err(|error| format!("请求 {url} 失败：{error}"))?;
        let status = response.status().as_u16();
        if !(200..300).contains(&status) {
            return Err(format!("服务器返回 HTTP {status}"));
        }
        // 读取少量响应，确保连接可以正常结束。
        let _ = response.body_mut().read_to_string();
        Ok(())
    }

    /// 流式下载到 `destination`，返回实际大小和 SHA-256。
    pub fn download(
        &self,
        url: &str,
        destination: &Path,
        maximum_size: u64,
        mut on_progress: impl FnMut(u64),
    ) -> Result<(u64, String), String> {
        if let Some(parent) = destination.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("无法创建缓存目录：{error}"))?;
        }
        let mut response = self
            .agent
            .get(url)
            .config()
            .https_only(url.starts_with("https://"))
            .timeout_global(Some(Duration::from_secs(600)))
            .timeout_connect(Some(Duration::from_secs(10)))
            .timeout_recv_response(Some(Duration::from_secs(15)))
            .timeout_per_read(Some(Duration::from_secs(20)))
            .build()
            .call()
            .map_err(|error| format!("下载 {url} 失败：{error}"))?;
        let status = response.status().as_u16();
        if !(200..300).contains(&status) {
            return Err(format!("下载地址返回 HTTP {status}"));
        }

        let mut file = File::create(destination)
            .map_err(|error| format!("无法创建 {}：{error}", destination.display()))?;
        let mut reader = response.body_mut().as_reader();
        let mut hasher = Sha256::new();
        let mut total = 0u64;
        let mut buffer = [0u8; 64 * 1024];
        loop {
            let read = reader
                .read(&mut buffer)
                .map_err(|error| format!("读取下载内容失败：{error}"))?;
            if read == 0 {
                break;
            }
            total += read as u64;
            if total > maximum_size {
                return Err("下载大小超过签名清单所列大小".into());
            }
            file.write_all(&buffer[..read])
                .map_err(|error| format!("写入更新包失败：{error}"))?;
            hasher.update(&buffer[..read]);
            on_progress(total);
        }
        file.sync_all()
            .map_err(|error| format!("写入更新包失败：{error}"))?;

        let digest: [u8; 32] = hasher.finalize().into();
        Ok((total, to_hex(&digest)))
    }
}

pub fn to_hex(bytes: &[u8]) -> String {
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
    fn converts_to_lowercase_hex() {
        assert_eq!(to_hex(&[0x00, 0x0f, 0xa5, 0xff]), "000fa5ff");
    }
}

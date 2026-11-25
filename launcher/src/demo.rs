use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use flate2::{Compression, write::GzEncoder};
use ring::{
    rand::SystemRandom,
    signature::{Ed25519KeyPair, KeyPair},
};
use sha2::{Digest, Sha256};

use crate::http::to_hex;
use crate::install::{Channel, InstallState};
use crate::manifest::{ManifestTrust, platform};
use crate::package;

pub const DEMO_INSTALLED: &str = "2026-01-01-1";
pub const DEMO_DEV: &str = "2026-01-02-1";
pub const DEMO_STABLE: &str = "2026-01-03-1";

/// 演示用的初始安装状态：用户选了 dev，当前实际安装的是 stable。
pub fn initial_install() -> InstallState {
    InstallState {
        channel: Channel::Dev,
        version: DEMO_INSTALLED.parse().expect("演示版本有效"),
        build_channel: Channel::Stable,
        active_bundle: None,
    }
}

struct Data {
    base: String,
    artifact: Vec<u8>,
    sha256: String,
    signer: Ed25519KeyPair,
    health_status: u16,
}

/// 只监听本机回环地址的演示服务器，提供健康检查、更新元数据、更新包和诊断接收。
pub struct MockServer {
    addr: SocketAddr,
    base: String,
    shutdown: Arc<AtomicBool>,
    received: Arc<AtomicUsize>,
    public_key: [u8; 32],
    thread: Option<JoinHandle<()>>,
}

impl MockServer {
    pub fn start() -> Result<Self, String> {
        let artifact = (0..1024 * 1024)
            .map(|index| (index * 31 % 251) as u8)
            .collect();
        Self::with_artifact(artifact)
    }

    pub fn with_subject(subject: &Path) -> Result<Self, String> {
        let result = (|| -> std::io::Result<Vec<u8>> {
            let mut archive = tar::Builder::new(GzEncoder::new(Vec::new(), Compression::fast()));
            archive.append_path_with_name(subject, package::executable(Path::new("")))?;
            archive.into_inner()?.finish()
        })();
        Self::with_artifact(result.map_err(|error| format!("无法创建演示更新包：{error}"))?)
    }

    pub fn with_artifact(artifact: Vec<u8>) -> Result<Self, String> {
        Self::serve(artifact, 200)
    }

    pub fn unavailable() -> Result<Self, String> {
        Self::serve(Vec::new(), 503)
    }

    pub fn empty() -> Result<Self, String> {
        Self::serve(Vec::new(), 200)
    }

    fn serve(artifact: Vec<u8>, health_status: u16) -> Result<Self, String> {
        platform()?;
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .map_err(|error| format!("无法启动演示服务器：{error}"))?;
        listener
            .set_nonblocking(true)
            .map_err(|error| format!("无法设置演示服务器：{error}"))?;
        let addr = listener
            .local_addr()
            .map_err(|error| format!("无法读取演示服务器地址：{error}"))?;
        let base = format!("http://{addr}");
        let sha256 = to_hex(&Sha256::digest(&artifact));
        let pkcs8 = Ed25519KeyPair::generate_pkcs8(&SystemRandom::new())
            .map_err(|_| "无法生成演示签名密钥".to_string())?;
        let signer = Ed25519KeyPair::from_pkcs8(pkcs8.as_ref())
            .map_err(|_| "无法读取演示签名密钥".to_string())?;
        let public_key = signer
            .public_key()
            .as_ref()
            .try_into()
            .expect("Ed25519 key has 32 bytes");
        let data = Arc::new(Data {
            base: base.clone(),
            artifact,
            sha256,
            signer,
            health_status,
        });
        let shutdown = Arc::new(AtomicBool::new(false));
        let received = Arc::new(AtomicUsize::new(0));

        let serve_shutdown = Arc::clone(&shutdown);
        let serve_received = Arc::clone(&received);
        let thread = thread::spawn(move || serve(listener, serve_shutdown, data, serve_received));
        Ok(Self {
            addr,
            base,
            shutdown,
            received,
            public_key,
            thread: Some(thread),
        })
    }

    pub fn base_url(&self) -> &str {
        &self.base
    }

    pub fn trust(&self) -> ManifestTrust {
        ManifestTrust::demo(self.public_key)
    }

    pub fn diagnostics_received(&self) -> usize {
        self.received.load(Ordering::SeqCst)
    }
}

impl Drop for MockServer {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
        // 唤醒可能在 accept 中等待的连接。
        let _ = TcpStream::connect(self.addr);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn serve(
    listener: TcpListener,
    shutdown: Arc<AtomicBool>,
    data: Arc<Data>,
    received: Arc<AtomicUsize>,
) {
    while !shutdown.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, _)) => {
                let _ = handle_connection(stream, &data, &received);
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(5));
            }
            Err(_) => break,
        }
    }
}

fn handle_connection(
    mut stream: TcpStream,
    data: &Data,
    received: &AtomicUsize,
) -> std::io::Result<()> {
    // accept() 在非阻塞监听器上返回的流可能也是非阻塞的，读超时不会生效。
    stream.set_nonblocking(false)?;
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    let Some((method, path, _body)) = read_request(&mut stream) else {
        return Ok(());
    };

    let (status, content_type, body, throttle) = match (method.as_str(), path.as_str()) {
        ("GET", "/health") => (data.health_status, "text/plain", b"health".to_vec(), false),
        ("GET", "/releases/dev.json") => (
            200,
            "application/json",
            manifest_json("dev", DEMO_DEV, data).into_bytes(),
            false,
        ),
        ("GET", "/releases/stable.json") => (
            200,
            "application/json",
            manifest_json("stable", DEMO_STABLE, data).into_bytes(),
            false,
        ),
        ("POST", "/diagnostics") => {
            received.fetch_add(1, Ordering::SeqCst);
            (200, "application/json", br#"{"ok":true}"#.to_vec(), false)
        }
        (_, path) if path.starts_with("/artifacts/") => {
            (200, "application/octet-stream", data.artifact.clone(), true)
        }
        _ => (404, "text/plain", b"not found".to_vec(), false),
    };
    write_response(&mut stream, status, content_type, &body, throttle)
}

fn read_request(stream: &mut TcpStream) -> Option<(String, String, Vec<u8>)> {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 4096];
    let header_end = loop {
        let read = stream.read(&mut chunk).ok()?;
        if read == 0 {
            return None;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(index) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
            break index;
        }
        if buffer.len() > 64 * 1024 {
            return None;
        }
    };

    let head = String::from_utf8_lossy(&buffer[..header_end]).into_owned();
    let mut lines = head.split("\r\n");
    let mut request_line = lines.next()?.split_whitespace();
    let method = request_line.next()?.to_string();
    let path = request_line.next()?.split('?').next()?.to_string();
    let content_length = lines
        .filter_map(|line| line.split_once(':'))
        .find_map(|(name, value)| {
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        })
        .unwrap_or(0);

    let mut body = buffer[header_end + 4..].to_vec();
    while body.len() < content_length {
        let read = stream.read(&mut chunk).ok()?;
        if read == 0 {
            break;
        }
        body.extend_from_slice(&chunk[..read]);
    }
    Some((method, path, body))
}

fn write_response(
    stream: &mut TcpStream,
    status: u16,
    content_type: &str,
    body: &[u8],
    throttle: bool,
) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        404 => "Not Found",
        _ => "Error",
    };
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )?;
    if throttle {
        for chunk in body.chunks(64 * 1024) {
            stream.write_all(chunk)?;
            stream.flush()?;
            thread::sleep(Duration::from_millis(16));
        }
    } else {
        stream.write_all(body)?;
    }
    stream.flush()
}

fn manifest_json(channel: &str, version: &str, data: &Data) -> String {
    let (os, arch) = platform().expect("演示模式已检查平台");
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let mut releases = Vec::new();
    if !data.artifact.is_empty() {
        releases.push(serde_json::json!({
            "channel": channel,
            "version": version,
            "os": os,
            "arch": arch,
            "url": format!("{}/artifacts/demo-{version}.tar.gz", data.base),
            "size": data.artifact.len(),
            "sha256": data.sha256,
        }));
    }
    let payload = serde_json::json!({
        "channel": channel,
        "issued_at": now,
        "expires_at": now + 3600,
        "releases": releases,
    })
    .to_string();
    let signature = data.signer.sign(payload.as_bytes());
    serde_json::json!({
        "schema": 1,
        "key_id": "demo",
        "payload": URL_SAFE_NO_PAD.encode(payload.as_bytes()),
        "signature": URL_SAFE_NO_PAD.encode(signature.as_ref()),
    })
    .to_string()
}

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

use minecrafttools_launcher::segmented::{self, Config};

struct RangeServer {
    base: String,
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    peak: Arc<AtomicUsize>,
    accept: Option<thread::JoinHandle<()>>,
}

impl RangeServer {
    fn start(payload: Vec<u8>, support_ranges: bool, delay: Duration) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let base = format!("http://{addr}");
        let payload = Arc::new(payload);
        let stop = Arc::new(AtomicBool::new(false));
        let active = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));

        let server_stop = Arc::clone(&stop);
        let server_peak = Arc::clone(&peak);
        let accept = thread::spawn(move || {
            while !server_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let payload = Arc::clone(&payload);
                        let active = Arc::clone(&active);
                        let peak = Arc::clone(&server_peak);
                        thread::spawn(move || {
                            let current = active.fetch_add(1, Ordering::SeqCst) + 1;
                            peak.fetch_max(current, Ordering::SeqCst);
                            let _ = handle(stream, &payload, support_ranges, delay);
                            active.fetch_sub(1, Ordering::SeqCst);
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => break,
                }
            }
        });

        Self {
            base,
            addr,
            stop,
            peak,
            accept: Some(accept),
        }
    }

    fn url(&self) -> String {
        format!("{}/file.bin", self.base)
    }

    fn peak(&self) -> usize {
        self.peak.load(Ordering::SeqCst)
    }
}

impl Drop for RangeServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(self.addr);
        if let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
    }
}

fn handle(
    mut stream: TcpStream,
    payload: &[u8],
    support_ranges: bool,
    delay: Duration,
) -> std::io::Result<()> {
    stream.set_nonblocking(false)?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    let Some((path, range)) = read_request(&mut stream) else {
        return Ok(());
    };
    if path != "/file.bin" {
        return respond(
            &mut stream,
            "404 Not Found",
            &[],
            b"not found",
            Duration::ZERO,
        );
    }

    if support_ranges && let Some((start, end)) = range {
        let end = end.min(payload.len() as u64 - 1);
        if start > end || start >= payload.len() as u64 {
            return respond(
                &mut stream,
                "416 Range Not Satisfiable",
                &[],
                b"",
                Duration::ZERO,
            );
        }
        let body = &payload[start as usize..=end as usize];
        let content_range = format!("bytes {start}-{end}/{}", payload.len());
        return respond(
            &mut stream,
            "206 Partial Content",
            &[
                ("Accept-Ranges", "bytes"),
                ("ETag", "\"test-etag\""),
                ("Content-Range", content_range.as_str()),
            ],
            body,
            delay,
        );
    }
    respond(
        &mut stream,
        "200 OK",
        &[("Accept-Ranges", "bytes"), ("ETag", "\"test-etag\"")],
        payload,
        delay,
    )
}

fn read_request(stream: &mut TcpStream) -> Option<(String, Option<(u64, u64)>)> {
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
        if buffer.len() > 16 * 1024 {
            return None;
        }
    };
    let head = String::from_utf8_lossy(&buffer[..header_end]).into_owned();
    let mut lines = head.split("\r\n");
    let path = lines.next()?.split_whitespace().nth(1)?.to_string();
    let mut range = None;
    for line in lines {
        if let Some((name, value)) = line.split_once(':')
            && name.eq_ignore_ascii_case("range")
            && let Some(value) = value.trim().strip_prefix("bytes=")
            && let Some((start, end)) = value.split_once('-')
        {
            range = Some((start.parse().ok()?, end.parse().ok()?));
        }
    }
    Some((path, range))
}

fn respond(
    stream: &mut TcpStream,
    status: &str,
    headers: &[(&str, &str)],
    body: &[u8],
    delay: Duration,
) -> std::io::Result<()> {
    write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    )?;
    for (name, value) in headers {
        write!(stream, "{name}: {value}\r\n")?;
    }
    write!(stream, "\r\n")?;
    for chunk in body.chunks(64 * 1024) {
        stream.write_all(chunk)?;
        stream.flush()?;
        if !delay.is_zero() {
            thread::sleep(delay);
        }
    }
    Ok(())
}

fn payload(size: usize) -> Vec<u8> {
    (0..size).map(|index| (index * 31 % 251) as u8).collect()
}

fn hex_digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[test]
fn ranged_download_ramps_up_and_matches_payload() {
    let data = payload(8 * 1024 * 1024);
    let server = RangeServer::start(data.clone(), true, Duration::from_millis(10));
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("file.bin");
    let config = Config {
        max_connections: 4,
        chunk_size: 1024 * 1024,
        ramp_interval: Duration::from_millis(40),
        request_timeout: Duration::from_secs(20),
        max_retries: 2,
    };

    let summary = segmented::download(&server.url(), &destination, &config, |_, _| {}).unwrap();

    assert!(!summary.fallback);
    assert_eq!(summary.bytes, data.len() as u64);
    assert_eq!(std::fs::read(&destination).unwrap(), data);
    assert_eq!(summary.sha256, hex_digest(&data));
    assert!(
        summary.connections_peak >= 3,
        "peak={}",
        summary.connections_peak
    );
    assert!(summary.ramp.len() >= 3, "ramp={:?}", summary.ramp);
    assert!(server.peak() >= 3, "server peak={}", server.peak());
}

#[test]
fn falls_back_when_server_ignores_range() {
    let data = payload(2 * 1024 * 1024);
    let server = RangeServer::start(data.clone(), false, Duration::from_millis(1));
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("file.bin");

    let summary =
        segmented::download(&server.url(), &destination, &Config::default(), |_, _| {}).unwrap();

    assert!(summary.fallback);
    assert_eq!(summary.bytes, data.len() as u64);
    assert_eq!(std::fs::read(&destination).unwrap(), data);
    assert_eq!(summary.sha256, hex_digest(&data));
}

#[test]
fn throttled_server_is_faster_with_more_connections() {
    let data = payload(8 * 1024 * 1024);
    let server = RangeServer::start(data.clone(), true, Duration::from_millis(10));
    let directory = tempfile::tempdir().unwrap();

    let single_config = Config {
        max_connections: 1,
        chunk_size: 1024 * 1024,
        ramp_interval: Duration::from_millis(10),
        request_timeout: Duration::from_secs(20),
        max_retries: 2,
    };
    let started = Instant::now();
    segmented::download(
        &server.url(),
        &directory.path().join("single.bin"),
        &single_config,
        |_, _| {},
    )
    .unwrap();
    let single = started.elapsed();

    let multi_config = Config {
        max_connections: 4,
        ..single_config
    };
    let started = Instant::now();
    let summary = segmented::download(
        &server.url(),
        &directory.path().join("multi.bin"),
        &multi_config,
        |_, _| {},
    )
    .unwrap();
    let multi = started.elapsed();
    eprintln!(
        "single={single:?} multi={multi:?} peak={} ramp={:?}",
        summary.connections_peak, summary.ramp
    );
    assert!(
        multi < single,
        "multi={multi:?} should beat single={single:?}"
    );
    assert_eq!(
        std::fs::read(directory.path().join("multi.bin")).unwrap(),
        data
    );
}

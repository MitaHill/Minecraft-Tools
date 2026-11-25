use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use crate::http::HttpClient;

pub const HEALTH_ATTEMPTS: u32 = 11;
pub const HEALTH_DELAY: Duration = Duration::from_secs(3);
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NetworkStatus {
    Online,
    Offline,
    Unknown,
}

/// 明确断网时返回 `Offline`；能解析但连不上时交给健康检查判断。
pub fn probe(host: &str, port: u16) -> NetworkStatus {
    let addresses = match (host, port).to_socket_addrs() {
        Ok(addresses) => addresses.collect::<Vec<_>>(),
        Err(_) => return NetworkStatus::Offline,
    };
    if addresses.is_empty() {
        return NetworkStatus::Offline;
    }
    for address in addresses {
        if TcpStream::connect_timeout(&address, PROBE_TIMEOUT).is_ok() {
            return NetworkStatus::Online;
        }
    }
    NetworkStatus::Unknown
}

/// 首次请求失败后最多重试 10 次，每次重试前等待 `delay`。
pub fn wait_for_health(
    client: &HttpClient,
    url: &str,
    attempts: u32,
    delay: Duration,
    mut on_failure: impl FnMut(u32, u32, &str),
) -> Result<(), String> {
    let attempts = attempts.max(1);
    let mut last_error = String::from("没有收到响应");
    for attempt in 1..=attempts {
        match client.get_status(url) {
            Ok(status) if (200..300).contains(&status) => return Ok(()),
            Ok(status) => last_error = format!("服务器返回 HTTP {status}"),
            Err(error) => last_error = error,
        }
        on_failure(attempt, attempts, &last_error);
        if attempt < attempts {
            std::thread::sleep(delay);
        }
    }
    Err(last_error)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn probe_finds_a_listening_port() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        assert_eq!(probe("127.0.0.1", port), NetworkStatus::Online);
    }

    #[test]
    fn health_retries_the_documented_number_of_times() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);

        let client = HttpClient::new(Duration::from_millis(200));
        let mut failures = 0;
        let result = wait_for_health(
            &client,
            &format!("http://127.0.0.1:{port}/health"),
            3,
            Duration::from_millis(5),
            |_, _, _| failures += 1,
        );
        assert!(result.is_err());
        assert_eq!(failures, 3);
    }
}

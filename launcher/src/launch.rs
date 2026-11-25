use std::fmt;
use std::io::{BufRead, Read};
use std::net::{TcpListener, TcpStream};
use std::process::Child;
use std::time::{Duration, Instant};

pub const READY_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug)]
pub enum LaunchError {
    EarlyExit(Option<i32>),
    Timeout,
    Io(String),
}

impl fmt::Display for LaunchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EarlyExit(Some(code)) => {
                write!(formatter, "主体程序在就绪前退出（退出码 {code}）")
            }
            Self::EarlyExit(None) => formatter.write_str("主体程序在就绪前退出"),
            Self::Timeout => formatter.write_str("30 秒内未收到主体程序的有效就绪信号"),
            Self::Io(reason) => write!(formatter, "等待主体程序就绪失败：{reason}"),
        }
    }
}

impl std::error::Error for LaunchError {}

/// 在临时端口监听一次性 READY 信号。端口和随机值通过子进程环境传入。
pub struct Handshake {
    listener: TcpListener,
    port: u16,
    token: String,
}

impl Handshake {
    pub fn bind() -> Result<Self, String> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .map_err(|error| format!("无法监听本机端口：{error}"))?;
        listener
            .set_nonblocking(true)
            .map_err(|error| format!("无法设置监听模式：{error}"))?;
        let port = listener
            .local_addr()
            .map_err(|error| format!("无法读取监听端口：{error}"))?
            .port();
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).map_err(|error| format!("无法生成启动令牌：{error}"))?;
        let mut token = String::with_capacity(32);
        for byte in random {
            token.push_str(&format!("{byte:02x}"));
        }
        Ok(Self {
            listener,
            port,
            token,
        })
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    pub fn wait_for_ready(&self, child: &mut Child, timeout: Duration) -> Result<(), LaunchError> {
        let deadline = Instant::now() + timeout;
        loop {
            if Instant::now() >= deadline {
                return Err(LaunchError::Timeout);
            }
            match self.listener.accept() {
                Ok((stream, _)) => {
                    if self.is_ready(stream, deadline)? {
                        return Ok(());
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    if let Some(status) = child
                        .try_wait()
                        .map_err(|error| LaunchError::Io(error.to_string()))?
                    {
                        return Err(LaunchError::EarlyExit(status.code()));
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                Err(error) => {
                    return Err(LaunchError::Io(format!("接收就绪信号失败：{error}")));
                }
            }
        }
    }

    fn is_ready(&self, stream: TcpStream, deadline: Instant) -> Result<bool, LaunchError> {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(false);
        }
        stream
            .set_nonblocking(false)
            .map_err(|error| LaunchError::Io(error.to_string()))?;
        stream
            .set_read_timeout(Some(remaining))
            .map_err(|error| LaunchError::Io(error.to_string()))?;
        let mut reader = std::io::BufReader::new(stream);
        let mut line = String::new();
        match reader.by_ref().take(128).read_line(&mut line) {
            Ok(0) => Ok(false),
            Ok(_) => Ok(line.trim_end_matches(['\r', '\n']) == format!("READY {}", self.token)),
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock
                    || error.kind() == std::io::ErrorKind::TimedOut =>
            {
                Ok(false)
            }
            Err(error) => Err(LaunchError::Io(error.to_string())),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_is_32_hex_characters() {
        let handshake = Handshake::bind().unwrap();
        assert_eq!(handshake.token().len(), 32);
        assert!(
            handshake
                .token()
                .chars()
                .all(|character| character.is_ascii_hexdigit())
        );
        assert_ne!(handshake.port(), 0);
    }
}

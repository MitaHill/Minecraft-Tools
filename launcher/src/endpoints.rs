use crate::install::Channel;

/// 总控制端地址及其固定路径。
#[derive(Clone, Debug)]
pub struct Endpoints {
    base: String,
    host: String,
    port: u16,
}

impl Endpoints {
    pub fn new(base: &str) -> Result<Self, String> {
        let base = base.trim_end_matches('/');
        let (rest, default_port) = if let Some(rest) = base.strip_prefix("https://") {
            (rest, 443)
        } else if let Some(rest) = base.strip_prefix("http://") {
            (rest, 80)
        } else {
            return Err("总控制端地址需要以 https:// 或 http:// 开头".into());
        };
        let (authority, _path) = rest.split_once('/').unwrap_or((rest, ""));
        let (host, port) = split_authority(authority, default_port)?;
        if host.is_empty() || authority.contains('@') || base.contains(['?', '#']) {
            return Err("总控制端地址无效".into());
        }
        if default_port == 80 && !matches!(host.as_str(), "127.0.0.1" | "::1" | "localhost") {
            return Err("总控制端地址需要 HTTPS".into());
        }
        Ok(Self {
            base: base.to_string(),
            host,
            port,
        })
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    pub fn host(&self) -> &str {
        &self.host
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn health(&self) -> String {
        format!("{}/health", self.base)
    }

    pub fn releases(&self, channel: Channel) -> String {
        format!("{}/releases/{}.json", self.base, channel.as_str())
    }

    pub fn diagnostics(&self) -> String {
        format!("{}/diagnostics", self.base)
    }
}

fn split_authority(authority: &str, default_port: u16) -> Result<(String, u16), String> {
    if let Some(rest) = authority.strip_prefix('[') {
        let (host, tail) = rest
            .split_once(']')
            .ok_or_else(|| "IPv6 地址缺少 ]".to_string())?;
        if tail.is_empty() {
            return Ok((host.to_string(), default_port));
        }
        let port = tail
            .strip_prefix(':')
            .ok_or_else(|| "IPv6 地址的端口格式错误".to_string())?;
        return Ok((host.to_string(), parse_port(port)?));
    }
    match authority.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() => Ok((host.to_string(), parse_port(port)?)),
        _ => Ok((authority.to_string(), default_port)),
    }
}

fn parse_port(value: &str) -> Result<u16, String> {
    value
        .parse::<u16>()
        .ok()
        .filter(|port| *port != 0)
        .ok_or_else(|| format!("端口无效：{value}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn derives_endpoint_paths() {
        let endpoints = Endpoints::new("https://example.com/").unwrap();
        assert_eq!(endpoints.base(), "https://example.com");
        assert_eq!(endpoints.health(), "https://example.com/health");
        assert_eq!(
            endpoints.releases(Channel::Dev),
            "https://example.com/releases/dev.json"
        );
        assert_eq!(endpoints.diagnostics(), "https://example.com/diagnostics");
        assert_eq!(endpoints.host(), "example.com");
        assert_eq!(endpoints.port(), 443);
    }

    #[test]
    fn keeps_explicit_ports_and_paths() {
        let endpoints = Endpoints::new("http://127.0.0.1:8123/api").unwrap();
        assert_eq!(endpoints.host(), "127.0.0.1");
        assert_eq!(endpoints.port(), 8123);
        assert_eq!(endpoints.health(), "http://127.0.0.1:8123/api/health");
    }

    #[test]
    fn rejects_invalid_addresses() {
        for value in [
            "example.com",
            "https://",
            "https://example.com:0",
            "https://example.com:abc",
            "http://example.com:8080",
            "https://user@example.com",
        ] {
            assert!(Endpoints::new(value).is_err(), "{value} should be rejected");
        }
    }
}

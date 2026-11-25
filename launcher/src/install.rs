use std::fmt;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::version::Version;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Channel {
    Stable,
    Dev,
}

impl Channel {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Stable => "stable",
            Self::Dev => "dev",
        }
    }
}

impl fmt::Display for Channel {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for Channel {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "stable" => Ok(Self::Stable),
            "dev" => Ok(Self::Dev),
            _ => Err(format!("未知更新通道：{value}")),
        }
    }
}

/// `install.json` 的内容：本机所选通道、实际安装的构建通道和版本。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstallState {
    pub channel: Channel,
    pub version: Version,
    pub build_channel: Channel,
    pub active_bundle: Option<String>,
}

#[derive(Debug)]
pub enum InstallError {
    Missing,
    Damaged(String),
}

impl fmt::Display for InstallError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing => formatter.write_str("找不到 install.json"),
            Self::Damaged(reason) => write!(formatter, "install.json 已损坏：{reason}"),
        }
    }
}

impl std::error::Error for InstallError {}

#[derive(Deserialize)]
struct RawInstall {
    channel: Channel,
    version: String,
    build_channel: Channel,
    active_bundle: Option<String>,
}

#[derive(Serialize)]
struct RawInstallOut {
    channel: Channel,
    version: String,
    build_channel: Channel,
    #[serde(skip_serializing_if = "Option::is_none")]
    active_bundle: Option<String>,
}

impl InstallState {
    pub fn load(path: &Path) -> Result<Self, InstallError> {
        let text = match fs::read_to_string(path) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Err(InstallError::Missing);
            }
            Err(error) => return Err(InstallError::Damaged(error.to_string())),
        };
        let raw: RawInstall = serde_json::from_str(&text)
            .map_err(|error| InstallError::Damaged(error.to_string()))?;
        let version = raw
            .version
            .parse()
            .map_err(|error: crate::version::ParseVersionError| {
                InstallError::Damaged(error.to_string())
            })?;
        if let Some(bundle) = &raw.active_bundle {
            let prefix = format!("{version}-");
            let suffix = bundle.strip_prefix(&prefix).unwrap_or_default();
            if suffix.is_empty()
                || suffix.len() > 64
                || !suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
            {
                return Err(InstallError::Damaged("无效的版本目录".into()));
            }
        }
        Ok(Self {
            channel: raw.channel,
            version,
            build_channel: raw.build_channel,
            active_bundle: raw.active_bundle,
        })
    }

    pub fn save(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("无法创建目录 {}：{error}", parent.display()))?;
        }
        let raw = RawInstallOut {
            channel: self.channel,
            version: self.version.to_string(),
            build_channel: self.build_channel,
            active_bundle: self.active_bundle.clone(),
        };
        let text = serde_json::to_string_pretty(&raw)
            .map_err(|error| format!("无法序列化 install.json：{error}"))?;
        let parent = path.parent().ok_or("安装状态缺少父目录")?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)
            .map_err(|error| format!("无法准备安装状态：{error}"))?;
        writeln!(temporary, "{text}")
            .and_then(|_| temporary.as_file().sync_all())
            .map_err(|error| format!("无法写入安装状态：{error}"))?;
        temporary
            .persist(path)
            .map_err(|error| format!("无法替换 {}：{error}", path.display()))?;
        let _ = crate::package::sync_directory(parent);
        Ok(())
    }

    /// 安装新版本后仍保留用户选择的通道，只更新实际安装的构建信息。
    pub fn with_update(&self, build_channel: Channel, version: Version, bundle: String) -> Self {
        Self {
            channel: self.channel,
            version,
            build_channel,
            active_bundle: Some(bundle),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state(channel: Channel, version: &str, build_channel: Channel) -> InstallState {
        InstallState {
            channel,
            version: version.parse().unwrap(),
            build_channel,
            active_bundle: None,
        }
    }

    #[test]
    fn round_trips_install_state() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("install.json");
        let original = state(Channel::Dev, "2026-11-25-1", Channel::Stable);
        original.save(&path).unwrap();
        assert_eq!(InstallState::load(&path).unwrap(), original);
    }

    #[test]
    fn reports_missing_install_file() {
        let directory = tempfile::tempdir().unwrap();
        assert!(matches!(
            InstallState::load(&directory.path().join("install.json")),
            Err(InstallError::Missing)
        ));
    }

    #[test]
    fn reports_damaged_install_file() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("install.json");
        for text in [
            "not json",
            r#"{"channel":"beta","version":"2026-11-25-1","build_channel":"stable"}"#,
            r#"{"channel":"dev","version":"0.0.0","build_channel":"stable"}"#,
        ] {
            fs::write(&path, text).unwrap();
            assert!(matches!(
                InstallState::load(&path),
                Err(InstallError::Damaged(_))
            ));
        }
    }

    #[test]
    fn update_keeps_selected_channel() {
        let installed = state(Channel::Dev, "2026-11-01-1", Channel::Stable);
        let updated = installed.with_update(
            Channel::Stable,
            "2026-11-02-1".parse().unwrap(),
            "2026-11-02-1-id".into(),
        );
        assert_eq!(updated.channel, Channel::Dev);
        assert_eq!(updated.build_channel, Channel::Stable);
        assert_eq!(updated.version.to_string(), "2026-11-02-1");
    }
}

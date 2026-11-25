use std::collections::{BTreeMap, HashSet};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ring::signature::{ED25519, UnparsedPublicKey};
use serde::Deserialize;

use crate::install::Channel;
use crate::version::Version;

pub const MAX_MANIFEST_BYTES: u64 = 512 * 1024;
const MAX_PACKAGE_BYTES: u64 = 1024 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize)]
pub struct ReleaseManifest {
    pub channel: Channel,
    pub issued_at: u64,
    pub expires_at: u64,
    pub releases: Vec<Release>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Release {
    pub channel: Channel,
    pub version: Version,
    pub os: String,
    pub arch: String,
    pub url: String,
    pub size: u64,
    pub sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SignedManifest {
    schema: u32,
    key_id: String,
    payload: String,
    signature: String,
}

pub struct ManifestTrust {
    keys: BTreeMap<String, [u8; 32]>,
    loopback_downloads: bool,
}

impl ManifestTrust {
    pub fn embedded() -> Result<Self, String> {
        Self::from_json(option_env!("MINECRAFTTOOLS_RELEASE_PUBLIC_KEYS").unwrap_or("{}"))
    }

    fn from_json(encoded: &str) -> Result<Self, String> {
        let keys: BTreeMap<String, String> =
            serde_json::from_str(encoded).map_err(|_| "内置发布公钥格式错误".to_string())?;
        let mut decoded = BTreeMap::new();
        for (id, value) in keys {
            let bytes = URL_SAFE_NO_PAD
                .decode(value)
                .map_err(|_| format!("无法解码发布公钥 {id}"))?;
            let key = bytes
                .try_into()
                .map_err(|_| format!("发布公钥 {id} 长度错误"))?;
            decoded.insert(id, key);
        }
        Ok(Self {
            keys: decoded,
            loopback_downloads: false,
        })
    }

    pub(crate) fn demo(key: [u8; 32]) -> Self {
        Self {
            keys: BTreeMap::from([("demo".into(), key)]),
            loopback_downloads: true,
        }
    }

    pub fn verify(&self, text: &str, channel: Channel) -> Result<ReleaseManifest, String> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "系统时间无效".to_string())?
            .as_secs();
        self.verify_at(text, channel, now)
    }

    fn verify_at(&self, text: &str, channel: Channel, now: u64) -> Result<ReleaseManifest, String> {
        let envelope: SignedManifest =
            serde_json::from_str(text).map_err(|_| "签名清单格式错误".to_string())?;
        if envelope.schema != 1 {
            return Err("不支持的更新清单格式".into());
        }
        let key = self
            .keys
            .get(&envelope.key_id)
            .ok_or("未信任的更新签名密钥")?;
        let payload = URL_SAFE_NO_PAD
            .decode(envelope.payload)
            .map_err(|_| "无法解码更新清单")?;
        let signature = URL_SAFE_NO_PAD
            .decode(envelope.signature)
            .map_err(|_| "无法解码更新清单签名")?;
        UnparsedPublicKey::new(&ED25519, key)
            .verify(&payload, &signature)
            .map_err(|_| "更新清单签名无效")?;
        let manifest: ReleaseManifest =
            serde_json::from_slice(&payload).map_err(|_| "已签名的更新清单内容格式错误")?;
        if manifest.channel != channel
            || manifest.issued_at > now.saturating_add(300)
            || manifest.expires_at <= now
            || manifest.expires_at <= manifest.issued_at
        {
            return Err("更新清单的通道或有效期不符".into());
        }
        let mut seen = HashSet::new();
        for release in &manifest.releases {
            if release.channel != channel
                || !matches!(release.os.as_str(), "darwin" | "linux" | "windows")
                || !matches!(release.arch.as_str(), "amd64" | "arm64")
                || release.size == 0
                || release.size > MAX_PACKAGE_BYTES
                || release.sha256.len() != 64
                || !release.sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
                || !self.valid_url(&release.url)
                || !seen.insert((
                    release.os.as_str(),
                    release.arch.as_str(),
                    release.version.to_string(),
                ))
            {
                return Err("更新清单包含无效或重复的发布记录".into());
            }
        }
        Ok(manifest)
    }

    fn valid_url(&self, value: &str) -> bool {
        let Ok(uri) = value.parse::<ureq::http::Uri>() else {
            return false;
        };
        let Some(authority) = uri.authority() else {
            return false;
        };
        if authority.as_str().contains('@') || authority.host().is_empty() {
            return false;
        }
        uri.scheme_str() == Some("https")
            || (self.loopback_downloads
                && uri.scheme_str() == Some("http")
                && matches!(
                    authority.host(),
                    "127.0.0.1" | "[::1]" | "::1" | "localhost"
                ))
    }
}

/// 与现有构建脚本一致的平台名称：`darwin`/`windows`/`linux` 和 `amd64`/`arm64`。
pub fn platform() -> Result<(&'static str, &'static str), String> {
    let os = match std::env::consts::OS {
        "macos" => "darwin",
        "windows" => "windows",
        "linux" => "linux",
        other => return Err(format!("不支持的平台：{other}")),
    };
    let arch = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => return Err(format!("不支持的架构：{other}")),
    };
    Ok((os, arch))
}

/// 只比较同一通道、当前系统和架构下，高于实际安装版本的发布。
pub fn select_release<'a>(
    releases: &'a [Release],
    channel: Channel,
    installed: Version,
    os: &str,
    arch: &str,
) -> Option<&'a Release> {
    releases
        .iter()
        .filter(|release| {
            release.channel == channel
                && release.os == os
                && release.arch == arch
                && release.version > installed
        })
        .max_by_key(|release| release.version)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::signature::{Ed25519KeyPair, KeyPair};

    fn manifest(releases: &str) -> ReleaseManifest {
        serde_json::from_str(&format!(
            r#"{{"channel":"dev","issued_at":1,"expires_at":2,"releases":[{releases}]}}"#
        ))
        .unwrap()
    }

    fn release(channel: &str, version: &str, os: &str, arch: &str) -> String {
        format!(
            r#"{{"channel":"{channel}","version":"{version}","os":"{os}","arch":"{arch}",
                "url":"https://example.com/a.zip","size":10,"sha256":"00"}}"#
        )
    }

    #[test]
    fn picks_the_highest_matching_release() {
        let versions = [
            release("dev", "2026-11-24-1", "darwin", "arm64"),
            release("dev", "2026-11-25-1", "darwin", "arm64"),
            release("stable", "2026-11-26-1", "darwin", "arm64"),
            release("dev", "2026-11-27-1", "darwin", "amd64"),
            release("dev", "2026-11-28-1", "windows", "arm64"),
        ]
        .join(",");
        let manifest = manifest(&versions);
        let installed: Version = "2026-11-23-1".parse().unwrap();
        let selected = select_release(
            &manifest.releases,
            Channel::Dev,
            installed,
            "darwin",
            "arm64",
        )
        .unwrap();
        assert_eq!(selected.version.to_string(), "2026-11-25-1");
    }

    #[test]
    fn ignores_releases_that_are_not_newer() {
        let releases = manifest(&release("dev", "2026-11-25-1", "darwin", "arm64"));
        let installed: Version = "2026-11-25-1".parse().unwrap();
        assert!(
            select_release(
                &releases.releases,
                Channel::Dev,
                installed,
                "darwin",
                "arm64"
            )
            .is_none()
        );
    }

    #[test]
    fn platform_matches_build_naming() {
        let (os, arch) = platform().unwrap();
        assert!(matches!(os, "darwin" | "windows" | "linux"));
        assert!(matches!(arch, "amd64" | "arm64"));
    }

    #[test]
    fn accepts_valid_empty_manifest_and_rejects_tampering_wrong_channel_and_expiry() {
        let key = Ed25519KeyPair::from_seed_unchecked(&[7u8; 32]).unwrap();
        let trust = ManifestTrust::demo(key.public_key().as_ref().try_into().unwrap());
        let payload = br#"{"channel":"dev","issued_at":1000,"expires_at":2000,"releases":[]}"#;
        let signature = key.sign(payload);
        let envelope = serde_json::json!({"schema":1,"key_id":"demo",
            "payload":URL_SAFE_NO_PAD.encode(payload),"signature":URL_SAFE_NO_PAD.encode(signature.as_ref())});
        assert!(
            trust
                .verify_at(&envelope.to_string(), Channel::Dev, 1100)
                .unwrap()
                .releases
                .is_empty()
        );
        assert!(
            trust
                .verify_at(&envelope.to_string(), Channel::Dev, 2000)
                .is_err()
        );
        assert!(
            trust
                .verify_at(&envelope.to_string(), Channel::Stable, 1100)
                .is_err()
        );
        assert!(
            trust
                .verify_at(std::str::from_utf8(payload).unwrap(), Channel::Dev, 1100)
                .is_err()
        );
        let mut wrong_key = envelope.clone();
        wrong_key["key_id"] = "unknown".into();
        assert!(
            trust
                .verify_at(&wrong_key.to_string(), Channel::Dev, 1100)
                .is_err()
        );
        let mut tampered = envelope;
        tampered["payload"] = URL_SAFE_NO_PAD
            .encode(br#"{"channel":"dev","issued_at":1000,"expires_at":2900,"releases":[]}"#)
            .into();
        assert!(
            trust
                .verify_at(&tampered.to_string(), Channel::Dev, 1100)
                .is_err()
        );
    }
}

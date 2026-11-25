use crate::endpoints::Endpoints;
use crate::http::HttpClient;
use crate::install::{Channel, InstallState};
use crate::manifest::{
    MAX_MANIFEST_BYTES, ManifestTrust, Release, ReleaseManifest, select_release,
};

pub trait ReleaseSource {
    fn manifest(&self, channel: Channel) -> Result<ReleaseManifest, String>;
}

/// 按文档中的通道规则选择更新：
/// 先查 dev，确认 dev 没有更高版本后才查 stable；dev 查询失败时结束本次检查。
pub fn find_update(
    source: &dyn ReleaseSource,
    installed: &InstallState,
    os: &str,
    arch: &str,
) -> Result<Option<Release>, String> {
    if installed.channel == Channel::Dev {
        let manifest = source.manifest(Channel::Dev)?;
        if let Some(release) = select_release(
            &manifest.releases,
            Channel::Dev,
            installed.version,
            os,
            arch,
        ) {
            return Ok(Some(release.clone()));
        }
    }
    let manifest = source.manifest(Channel::Stable)?;
    Ok(select_release(
        &manifest.releases,
        Channel::Stable,
        installed.version,
        os,
        arch,
    )
    .cloned())
}

pub struct HttpReleaseSource<'a> {
    pub client: &'a HttpClient,
    pub endpoints: &'a Endpoints,
    pub trust: &'a ManifestTrust,
}

impl ReleaseSource for HttpReleaseSource<'_> {
    fn manifest(&self, channel: Channel) -> Result<ReleaseManifest, String> {
        let url = self.endpoints.releases(channel);
        let text = self
            .client
            .get_text_bounded(&url, MAX_MANIFEST_BYTES)
            .map_err(|error| format!("读取 {channel} 通道失败：{error}"))?;
        self.trust
            .verify(&text, channel)
            .map_err(|error| format!("{channel} 通道：{error}"))
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::version::Version;

    fn release(channel: Channel, version: &str) -> Release {
        Release {
            channel,
            version: version.parse().unwrap(),
            os: "darwin".into(),
            arch: "arm64".into(),
            url: "https://example.com/a.zip".into(),
            size: 10,
            sha256: "00".into(),
        }
    }

    fn manifest(channel: Channel, releases: Vec<Release>) -> ReleaseManifest {
        ReleaseManifest {
            channel,
            issued_at: 1,
            expires_at: 2,
            releases,
        }
    }

    struct FakeSource {
        dev: Result<ReleaseManifest, String>,
        stable: Result<ReleaseManifest, String>,
        calls: RefCell<Vec<Channel>>,
    }

    impl ReleaseSource for FakeSource {
        fn manifest(&self, channel: Channel) -> Result<ReleaseManifest, String> {
            self.calls.borrow_mut().push(channel);
            match channel {
                Channel::Dev => self.dev.clone(),
                Channel::Stable => self.stable.clone(),
            }
        }
    }

    fn installed(channel: Channel, version: &str) -> InstallState {
        InstallState {
            channel,
            version: version.parse().unwrap(),
            build_channel: channel,
            active_bundle: None,
        }
    }

    #[test]
    fn stable_selected_only_checks_stable() {
        let source = FakeSource {
            dev: Ok(manifest(
                Channel::Dev,
                vec![release(Channel::Dev, "2026-12-01-1")],
            )),
            stable: Ok(manifest(
                Channel::Stable,
                vec![release(Channel::Stable, "2026-11-02-1")],
            )),
            calls: RefCell::new(Vec::new()),
        };
        let update = find_update(
            &source,
            &installed(Channel::Stable, "2026-11-01-1"),
            "darwin",
            "arm64",
        )
        .unwrap()
        .unwrap();
        assert_eq!(update.channel, Channel::Stable);
        assert_eq!(*source.calls.borrow(), vec![Channel::Stable]);
    }

    #[test]
    fn dev_wins_even_when_stable_is_higher() {
        let source = FakeSource {
            dev: Ok(manifest(
                Channel::Dev,
                vec![release(Channel::Dev, "2026-11-02-1")],
            )),
            stable: Ok(manifest(
                Channel::Stable,
                vec![release(Channel::Stable, "2026-11-03-1")],
            )),
            calls: RefCell::new(Vec::new()),
        };
        let update = find_update(
            &source,
            &installed(Channel::Dev, "2026-11-01-1"),
            "darwin",
            "arm64",
        )
        .unwrap()
        .unwrap();
        assert_eq!(update.channel, Channel::Dev);
        assert_eq!(*source.calls.borrow(), vec![Channel::Dev]);
    }

    #[test]
    fn dev_falls_back_to_stable_and_keeps_selection() {
        let source = FakeSource {
            dev: Ok(manifest(
                Channel::Dev,
                vec![release(Channel::Dev, "2026-11-01-1")],
            )),
            stable: Ok(manifest(
                Channel::Stable,
                vec![release(Channel::Stable, "2026-11-02-1")],
            )),
            calls: RefCell::new(Vec::new()),
        };
        let current = installed(Channel::Dev, "2026-11-01-1");
        let update = find_update(&source, &current, "darwin", "arm64")
            .unwrap()
            .unwrap();
        assert_eq!(update.channel, Channel::Stable);
        assert_eq!(current.channel, Channel::Dev);
        assert_eq!(*source.calls.borrow(), vec![Channel::Dev, Channel::Stable]);
    }

    #[test]
    fn dev_failure_does_not_fall_back() {
        let source = FakeSource {
            dev: Err("network".into()),
            stable: Ok(manifest(
                Channel::Stable,
                vec![release(Channel::Stable, "2026-11-09-1")],
            )),
            calls: RefCell::new(Vec::new()),
        };
        let error = find_update(
            &source,
            &installed(Channel::Dev, "2026-11-01-1"),
            "darwin",
            "arm64",
        )
        .unwrap_err();
        assert_eq!(error, "network");
        assert_eq!(*source.calls.borrow(), vec![Channel::Dev]);
    }

    #[test]
    fn no_newer_version_returns_none() {
        let source = FakeSource {
            dev: Ok(manifest(
                Channel::Dev,
                vec![release(Channel::Dev, "2026-11-01-1")],
            )),
            stable: Ok(manifest(
                Channel::Stable,
                vec![release(Channel::Stable, "2026-11-01-1")],
            )),
            calls: RefCell::new(Vec::new()),
        };
        let version: Version = "2026-11-01-1".parse().unwrap();
        assert!(
            find_update(
                &source,
                &installed(Channel::Dev, &version.to_string()),
                "darwin",
                "arm64"
            )
            .unwrap()
            .is_none()
        );
    }
}

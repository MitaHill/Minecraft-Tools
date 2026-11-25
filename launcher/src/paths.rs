use std::env;
use std::fs;
use std::path::PathBuf;

/// 配置、安装状态、日志和更新缓存的位置。
#[derive(Clone, Debug)]
pub struct Paths {
    pub config: PathBuf,
    pub state: PathBuf,
    pub logs: PathBuf,
    pub cache: PathBuf,
}

impl Paths {
    /// 按文档中的平台目录布局定位。`MINECRAFTTOOLS_DATA_ROOT` 供演示和测试覆盖根目录。
    pub fn discover() -> Result<Self, String> {
        if let Some(root) = env::var_os("MINECRAFTTOOLS_DATA_ROOT") {
            return Ok(Self::from_root(PathBuf::from(root)));
        }
        if cfg!(target_os = "windows") {
            let local = env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .or_else(|| {
                    env::var_os("USERPROFILE")
                        .map(|home| PathBuf::from(home).join("AppData").join("Local"))
                })
                .ok_or("无法确定 Windows 本地应用数据目录")?;
            let root = local.join("MitaHill").join("MinecraftTools");
            return Ok(Self {
                config: root.clone(),
                state: root.clone(),
                logs: root.join("logs"),
                cache: root.join("cache"),
            });
        }
        if cfg!(target_os = "macos") {
            let home = home_dir()?;
            return Ok(Self {
                config: home.join("Library/Application Support/com.mitahill.minecrafttools"),
                state: home.join("Library/Application Support/com.mitahill.minecrafttools"),
                logs: home.join("Library/Logs/com.mitahill.minecrafttools"),
                cache: home.join("Library/Caches/com.mitahill.minecrafttools"),
            });
        }
        if cfg!(target_os = "linux") {
            let home = home_dir()?;
            let config_root = env::var_os("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(".config"));
            let state_root = env::var_os("XDG_STATE_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(".local").join("state"));
            let cache_root = env::var_os("XDG_CACHE_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(".cache"));
            let state = state_root.join("minecrafttools");
            return Ok(Self {
                config: config_root.join("minecrafttools"),
                logs: state.join("logs"),
                state,
                cache: cache_root.join("minecrafttools"),
            });
        }
        Err("当前系统不在支持范围内".into())
    }

    /// 演示和测试使用单一根目录，避免写入真实用户数据。
    pub fn from_root(root: impl Into<PathBuf>) -> Self {
        let root = root.into();
        Self {
            config: root.clone(),
            state: root.clone(),
            logs: root.join("logs"),
            cache: root.join("cache"),
        }
    }

    pub fn install_file(&self) -> PathBuf {
        self.state.join("install.json")
    }

    pub fn versions(&self) -> PathBuf {
        self.state.join("versions")
    }

    pub fn launcher_log(&self) -> PathBuf {
        self.logs.join("launcher.log")
    }

    pub fn client_log(&self) -> PathBuf {
        self.logs.join("client.log")
    }

    pub fn ensure(&self) -> Result<(), String> {
        for directory in [&self.config, &self.state, &self.logs, &self.cache] {
            fs::create_dir_all(directory)
                .map_err(|error| format!("无法创建目录 {}：{error}", directory.display()))?;
        }
        Ok(())
    }
}

fn home_dir() -> Result<PathBuf, String> {
    env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "无法确定用户主目录".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn root_layout_keeps_logs_and_cache_separate() {
        let paths = Paths::from_root("/tmp/example");
        assert_eq!(
            paths.install_file(),
            PathBuf::from("/tmp/example/install.json")
        );
        assert_eq!(
            paths.launcher_log(),
            PathBuf::from("/tmp/example/logs/launcher.log")
        );
        assert_eq!(
            paths.client_log(),
            PathBuf::from("/tmp/example/logs/client.log")
        );
        assert_eq!(paths.cache, PathBuf::from("/tmp/example/cache"));
    }

    #[test]
    fn ensure_creates_directories() {
        let directory = tempfile::tempdir().unwrap();
        let paths = Paths::from_root(directory.path());
        paths.ensure().unwrap();
        for path in [&paths.config, &paths.state, &paths.logs, &paths.cache] {
            assert!(path.is_dir(), "{} should exist", path.display());
        }
    }
}

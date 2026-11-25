use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader};
use std::path::{Component, Path, PathBuf};

use flate2::read::GzDecoder;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tempfile::TempDir;

use crate::http::to_hex;
use crate::install::Channel;
use crate::version::Version;

const MAX_UNPACKED_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const MAX_ENTRIES: usize = 100_000;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bootstrap {
    pub version: Version,
    pub channel: Channel,
    size: u64,
    sha256: String,
    #[serde(skip)]
    archive: PathBuf,
}

impl Bootstrap {
    pub fn discover(launcher: &Path) -> Result<Option<Self>, String> {
        let parent = launcher.parent().ok_or("无法定位安装目录")?;
        let resources = if cfg!(target_os = "macos") && parent.ends_with("Contents/MacOS") {
            parent.with_file_name("Resources")
        } else {
            parent.to_path_buf()
        };
        let metadata = resources.join("bootstrap.json");
        let text = match fs::read_to_string(metadata) {
            Ok(text) => text,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(format!("无法读取安装包信息：{error}")),
        };
        let mut bootstrap: Self =
            serde_json::from_str(&text).map_err(|error| format!("安装包信息无效：{error}"))?;
        bootstrap.archive = resources.join("bootstrap.tar.gz");
        Ok(Some(bootstrap))
    }

    pub fn stage(&self, versions: &Path) -> io::Result<TempDir> {
        let mut file = BufReader::new(File::open(&self.archive)?);
        if self.size == 0
            || self.size > 1024 * 1024 * 1024
            || file.get_ref().metadata()?.len() != self.size
        {
            return Err(invalid("安装包主体文件大小不符"));
        }
        let mut hash = Sha256::new();
        io::copy(&mut file, &mut hash)?;
        if !to_hex(&hash.finalize()).eq_ignore_ascii_case(&self.sha256) {
            return Err(invalid("安装包主体文件校验失败"));
        }
        stage(&self.archive, versions, self.version)
    }
}

pub fn executable(root: &Path) -> PathBuf {
    if cfg!(target_os = "macos") {
        root.join("MinecraftTools.app/Contents/MacOS/MinecraftTools")
    } else if cfg!(target_os = "windows") {
        root.join("MinecraftTools/MinecraftTools.exe")
    } else {
        root.join("MinecraftTools/MinecraftTools")
    }
}

pub fn stage(source: &Path, versions: &Path, version: Version) -> io::Result<TempDir> {
    fs::create_dir_all(versions)?;
    let candidate = tempfile::Builder::new()
        .prefix(&format!("{version}-"))
        .tempdir_in(versions)?;
    extract(source, candidate.path())?;
    Ok(candidate)
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn safe_path(path: &Path, base: &Path, allow_parent: bool) -> io::Result<PathBuf> {
    let mut result = base.to_path_buf();
    for part in path.components() {
        match part {
            Component::Normal(value) => {
                let segment = value
                    .to_str()
                    .ok_or_else(|| invalid("更新包路径不是 UTF-8"))?;
                if segment.contains(['\\', ':']) || segment.ends_with(['.', ' ']) {
                    return Err(invalid("更新包路径包含不支持的字符"));
                }
                result.push(value);
            }
            Component::CurDir => {}
            Component::ParentDir if allow_parent && result.pop() => {}
            _ => return Err(invalid("更新包路径超出安装目录")),
        }
        if result.components().count() > 64 {
            return Err(invalid("更新包目录层级过深"));
        }
    }
    if result.as_os_str().is_empty() {
        return Err(invalid("更新包路径为空"));
    }
    Ok(result)
}

fn extract(source: &Path, root: &Path) -> io::Result<()> {
    let decoder = GzDecoder::new(BufReader::new(File::open(source)?));
    let mut archive = tar::Archive::new(decoder);
    let subject = executable(root);
    let bundle = executable(Path::new(""));
    let bundle = bundle.components().next().expect("executable has a bundle");
    let mut seen = HashSet::new();
    let mut links = Vec::new();
    let mut total = 0u64;
    for entry in archive.entries()? {
        let mut entry = entry?;
        let name = safe_path(&entry.path()?, Path::new(""), false)?;
        if name.components().next() != Some(bundle) || !seen.insert(name.clone()) {
            return Err(invalid("更新包文件超出应用目录或包含重复路径"));
        }
        if seen.len() > MAX_ENTRIES {
            return Err(invalid("更新包包含过多文件"));
        }
        let destination = root.join(&name);
        let parent = name.parent().expect("relative path has a parent");
        let kind = entry.header().entry_type();
        if kind.is_dir() {
            fs::create_dir_all(&destination)?;
            continue;
        }
        fs::create_dir_all(root.join(parent))?;
        if kind.is_file() {
            let size = entry.size();
            total = total
                .checked_add(size)
                .ok_or_else(|| invalid("更新包解压后过大"))?;
            if total > MAX_UNPACKED_BYTES {
                return Err(invalid("更新包解压后过大"));
            }
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&destination)?;
            if io::copy(&mut entry, &mut output)? != size {
                return Err(invalid("更新包文件长度不符"));
            }
            set_permissions(&output, entry.header().mode()?)?;
            output.sync_all()?;
        } else if kind.is_symlink() || kind.is_hard_link() {
            let target = entry
                .link_name()?
                .ok_or_else(|| invalid("更新包链接没有目标"))?
                .into_owned();
            let link_base = if kind.is_symlink() {
                parent
            } else {
                Path::new("")
            };
            let normalized = safe_path(&target, link_base, kind.is_symlink())?;
            if normalized.components().next() != Some(bundle) {
                return Err(invalid("更新包链接超出应用目录"));
            }
            links.push((kind, destination, target, normalized));
        } else {
            return Err(invalid("更新包包含不支持的文件类型"));
        }
    }
    // Regular files are complete before links exist, so a link cannot redirect extraction.
    for (kind, path, _, target) in &links {
        if kind.is_hard_link() {
            let source = root.join(target);
            if !fs::symlink_metadata(&source)?.is_file() {
                return Err(invalid("更新包硬链接的目标不是文件"));
            }
            fs::hard_link(source, path)?;
        }
    }
    for (kind, path, target, _) in &links {
        if kind.is_symlink() {
            make_symlink(target, path)?;
        }
    }
    if !fs::symlink_metadata(&subject).is_ok_and(|metadata| metadata.is_file()) {
        return Err(invalid("更新包缺少主体程序"));
    }
    let canonical_bundle = root.canonicalize()?.join(bundle.as_os_str());
    for (_, path, _, _) in links {
        if !path.canonicalize()?.starts_with(&canonical_bundle) {
            return Err(invalid("更新包链接超出应用目录"));
        }
    }
    sync_directories(root)?;
    sync_directory(root.parent().expect("candidate has a parent"))
}

#[cfg(unix)]
fn set_permissions(file: &File, mode: u32) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let mode = if mode & 0o111 != 0 { 0o755 } else { 0o644 };
    file.set_permissions(fs::Permissions::from_mode(mode))
}

#[cfg(not(unix))]
fn set_permissions(_: &File, _: u32) -> io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn make_symlink(target: &Path, path: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(target, path)
}

#[cfg(not(unix))]
fn make_symlink(_: &Path, _: &Path) -> io::Result<()> {
    Err(invalid("该平台的更新包不支持符号链接"))
}

#[cfg(unix)]
fn sync_directories(path: &Path) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            sync_directories(&entry.path())?;
        }
    }
    sync_directory(path)
}

#[cfg(unix)]
pub(crate) fn sync_directory(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

#[cfg(not(unix))]
fn sync_directories(_: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(not(unix))]
pub(crate) fn sync_directory(_: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use flate2::{Compression, write::GzEncoder};
    use tar::{Builder, EntryType, Header};

    #[test]
    fn preserves_internal_links_and_refuses_escaping_links() {
        let directory = tempfile::tempdir().unwrap();
        let archive_path = directory.path().join("bundle.tar.gz");
        let executable = executable(Path::new(""));
        let parent = executable.parent().unwrap();
        let versions = directory.path().join("versions");
        for (target, allowed) in [
            (Path::new("MinecraftTools"), true),
            (Path::new("../../../../outside"), false),
        ] {
            let file = File::create(&archive_path).unwrap();
            let mut archive = Builder::new(GzEncoder::new(file, Compression::fast()));
            let mut file_header = Header::new_gnu();
            file_header.set_size(4);
            file_header.set_mode(0o755);
            archive
                .append_data(&mut file_header, &executable, &b"file"[..])
                .unwrap();
            let mut link = Header::new_gnu();
            link.set_entry_type(EntryType::Symlink);
            link.set_size(0);
            archive
                .append_link(&mut link, parent.join("alias"), target)
                .unwrap();
            archive.into_inner().unwrap().finish().unwrap();
            let result = stage(&archive_path, &versions, "2026-11-25-2".parse().unwrap());
            assert_eq!(result.is_ok(), allowed, "{target:?}: {result:?}");
            if let Ok(staged) = result {
                let alias = staged.path().join(parent).join("alias");
                assert_eq!(fs::read_link(&alias).unwrap(), target);
                assert_eq!(fs::read(alias).unwrap(), b"file");
            }
        }
        assert_eq!(fs::read_dir(&versions).unwrap().count(), 0);
    }
}

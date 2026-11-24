use crate::native_update::{self, SegmentAction, MAX_PACKAGE};
use reqwest::{header, StatusCode};
use std::{
    fs::File,
    io::{self, Read, Seek, Write},
    path::Path,
};
#[cfg(unix)]
use std::{
    fs,
    mem::size_of,
    os::fd::{AsRawFd, FromRawFd},
};

const LIMIT: u64 = MAX_PACKAGE as u64;
#[cfg(unix)]
const CHECKSUM_BYTES: usize = 64;
#[cfg(unix)]
const PART_HEADER: u64 = (CHECKSUM_BYTES + size_of::<u64>()) as u64;

/// One private, bounded checkpoint per application cache. A second process
/// can download without a checkpoint while the lease is held.
struct DownloadLease {
    #[cfg(unix)]
    directory: File,
}

#[cfg(unix)]
impl DownloadLease {
    fn acquire(cache: &Path) -> Option<Self> {
        use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};

        fs::create_dir_all(cache).ok()?;
        let path = cache.join("legacy-dmg-download");
        match fs::DirBuilder::new().mode(0o700).create(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(_) => return None,
        }
        let directory = File::options()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_DIRECTORY)
            .open(&path)
            .ok()?;
        let metadata = directory.metadata().ok()?;
        // SAFETY: geteuid has no parameters and returns the process's effective UID.
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o077 != 0 {
            return None;
        }
        fs2::FileExt::try_lock_exclusive(&directory).ok()?;
        if !native_update::same_identity(&metadata, &path.symlink_metadata().ok()?) {
            return None;
        }
        Some(Self { directory })
    }

    // Use the directory held by the lease for all checkpoint I/O.
    fn open(&self, name: &std::ffi::CStr, flags: libc::c_int) -> io::Result<File> {
        // SAFETY: The directory descriptor and the NUL-terminated name are valid.
        let fd = unsafe {
            libc::openat(self.directory.as_raw_fd(), name.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC, 0o600 as libc::c_int)
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: A successful openat returns a fresh descriptor owned by this call.
        Ok(unsafe { File::from_raw_fd(fd) })
    }

    fn remove(&self, name: &std::ffi::CStr) {
        // SAFETY: The descriptor and the NUL-terminated name are valid.
        unsafe { libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0) };
    }

    fn restore(&self, expected: &str, staged: &mut File) -> io::Result<Option<(u64, Option<u64>)>> {
        use std::os::unix::fs::MetadataExt;
        let mut part = self.open(c"image.part", libc::O_RDONLY)?;
        let metadata = part.metadata()?;
        let size = metadata.len();
        if !metadata.is_file() || !native_update::single_link(&metadata)
            || metadata.uid() != self.directory.metadata()?.uid() || metadata.mode() & 0o077 != 0
            || size <= PART_HEADER || size > LIMIT + PART_HEADER
        {
            return Ok(None);
        }
        let mut checksum = [0; CHECKSUM_BYTES];
        let mut total = [0; size_of::<u64>()];
        part.read_exact(&mut checksum)?;
        if checksum.as_slice() != expected.as_bytes() {
            return Ok(None);
        }
        part.read_exact(&mut total)?;
        let total = match u64::from_le_bytes(total) {
            0 => None,
            value if value <= LIMIT => Some(value),
            _ => return Ok(None),
        };
        let offset = native_update::resume_offset(size - PART_HEADER, total);
        if offset == 0 {
            return Ok(None);
        }
        // Cache bytes enter the same private staging file as the new bytes.
        // The complete staged image will be checked before it is attached.
        let copied = io::copy(&mut part.take(offset + 1), staged)?;
        if copied != offset {
            return Ok(None);
        }
        Ok(Some((offset, total)))
    }

    fn save(&self, expected: &str, staged: &mut File, offset: u64, total: Option<u64>) {
        let pending = c"image.part.next";
        self.remove(pending);
        let result = (|| -> io::Result<()> {
            let mut part = self.open(pending, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
            part.write_all(expected.as_bytes())?;
            part.write_all(&total.unwrap_or(0).to_le_bytes())?;
            staged.rewind()?;
            if io::copy(&mut staged.take(offset), &mut part)? != offset {
                return Err(io::ErrorKind::UnexpectedEof.into());
            }
            part.sync_all()?;
            let dir = self.directory.as_raw_fd();
            // SAFETY: Both names are NUL-terminated and dir is held by the lease.
            if unsafe { libc::renameat(dir, pending.as_ptr(), dir, c"image.part".as_ptr()) } != 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        })();
        if result.is_err() {
            self.remove(pending);
        }
    }

    fn clear(&self) {
        self.remove(c"image.part");
        self.remove(c"image.part.next");
    }
}

#[cfg(not(unix))]
impl DownloadLease {
    fn acquire(_: &Path) -> Option<Self> { None }
    fn restore(&self, _: &str, _: &mut File) -> io::Result<Option<(u64, Option<u64>)>> { Ok(None) }
    fn save(&self, _: &str, _: &mut File, _: u64, _: Option<u64>) {}
    fn clear(&self) {}
}

pub(super) struct DownloadResponse<R> {
    pub(super) status: StatusCode,
    pub(super) headers: header::HeaderMap,
    pub(super) length: Option<u64>,
    pub(super) body: R,
}

pub(super) fn download<R: Read>(
    staged: &mut File,
    cache: Option<&Path>,
    expected: &str,
    mut fetch: impl FnMut(Option<String>) -> Result<DownloadResponse<R>, &'static str>,
    mut progress: impl FnMut(u64, u64),
) -> Result<(), &'static str> {
    let lease = cache.and_then(DownloadLease::acquire);
    let restored = lease.as_ref().and_then(|lease| lease.restore(expected, staged).ok().flatten());
    let (mut offset, mut total) = restored.unwrap_or((0, None));
    if restored.is_none() {
        reset(staged)?;
        if let Some(lease) = &lease {
            lease.clear();
        }
    }
    let result = loop {
        let response = match fetch(native_update::range_header(offset)) {
            Ok(response) => response,
            Err(error) => {
                if offset > restored.map_or(0, |(offset, _)| offset) {
                    if let Some(lease) = &lease {
                        lease.save(expected, staged, offset, total);
                    }
                }
                return Err(error);
            }
        };
        let DownloadResponse { status, headers, length, mut body } = response;
        if offset > 0 && (status == StatusCode::OK || status == StatusCode::RANGE_NOT_SATISFIABLE) {
            if let Some(lease) = &lease {
                lease.clear();
            }
            reset(staged)?;
            offset = 0;
            total = None;
            if status == StatusCode::RANGE_NOT_SATISFIABLE {
                continue;
            }
        }
        let (declared_total, end) = match response_bounds(status, &headers, length, offset, total) {
            Ok(bounds) => bounds,
            Err(error) => break Err(error),
        };
        total = declared_total;
        if let Some(total) = total {
            progress(offset, total);
        }
        let start = offset;
        let mut buffer = [0; 65536];
        loop {
            let read = match body.read(&mut buffer) {
                Ok(read) => read,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => return interrupted(&lease, expected, staged, offset, total),
            };
            if read == 0 {
                break;
            }
            let next = offset + read as u64;
            if next > LIMIT || end.is_some_and(|end| next > end) {
                if let Some(lease) = &lease {
                    lease.clear();
                }
                return Err(if next > LIMIT { "Download exceeds size limit" } else { "Invalid download range" });
            }
            staged.write_all(&buffer[..read]).map_err(|_| "Cannot write download")?;
            offset = next;
            if let Some(total) = total {
                progress(offset, total);
            }
        }
        if offset == start || end.is_some_and(|end| offset != end) {
            return interrupted(&lease, expected, staged, offset, total);
        }
        if total.is_none_or(|total| offset == total) {
            break Ok(());
        }
    };
    if let Some(lease) = &lease {
        lease.clear();
    }
    result
}

fn reset(staged: &mut File) -> Result<(), &'static str> {
    staged.set_len(0).and_then(|_| staged.rewind()).map_err(|_| "Cannot write download")
}

fn interrupted(
    lease: &Option<DownloadLease>,
    expected: &str,
    staged: &mut File,
    offset: u64,
    total: Option<u64>,
) -> Result<(), &'static str> {
    if let Some(lease) = lease.as_ref().filter(|_| offset > 0) {
        lease.save(expected, staged, offset, total);
    }
    Err("Download interrupted")
}

fn response_bounds(
    status: StatusCode,
    headers: &header::HeaderMap,
    length: Option<u64>,
    offset: u64,
    total: Option<u64>,
) -> Result<(Option<u64>, Option<u64>), &'static str> {
    const INVALID: &str = "Invalid download range";
    if headers.get_all(header::CONTENT_ENCODING).iter().any(|value| value != "identity") {
        return Err(INVALID);
    }
    let (declared_total, end) = match status {
        StatusCode::OK if !headers.contains_key(header::CONTENT_RANGE) => (length, length),
        StatusCode::PARTIAL_CONTENT => {
            let ranges = headers.get_all(header::CONTENT_RANGE);
            let mut values = ranges.iter();
            let (range, end) = values.next().and_then(|value| value.to_str().ok())
                .and_then(native_update::parse_segment_range).ok_or(INVALID)?;
            if values.next().is_some()
                || native_update::segment_action(offset, status.as_u16(), Some(&range)) != SegmentAction::Append
                || length.is_some_and(|length| length != end - offset)
                || total.zip(range.total).is_some_and(|(previous, current)| previous != current)
            {
                return Err(INVALID);
            }
            let total = range.total.or(total).ok_or(INVALID)?;
            if end > total {
                return Err(INVALID);
            }
            (Some(total), Some(end))
        }
        _ => return Err("Download failed"),
    };
    if declared_total.is_some_and(|total| total > LIMIT) {
        return Err("Download exceeds size limit");
    }
    if declared_total == Some(0) {
        return Err("Download interrupted");
    }
    Ok((declared_total, end))
}

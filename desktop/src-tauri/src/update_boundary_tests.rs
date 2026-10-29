use super::*;

#[test]
fn project_import_preserves_the_runtime_clone_deadline() {
    assert_eq!(transport::request_timeout("projects/import"), Duration::from_secs(210));
    assert_eq!(transport::request_timeout("projects/import?fixture=1"), Duration::from_secs(210));
    assert_eq!(transport::request_timeout("model-center/probe"), Duration::from_secs(960));
    assert_eq!(transport::request_timeout("state"), Duration::from_secs(60));
}

#[test]
fn updater_feed_wait_can_be_cancelled_before_transport_deadline() {
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    runtime.block_on(async {
        let cancel = AtomicBool::new(false);
        let operation = async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            cancel.store(true, Ordering::SeqCst);
            std::future::pending::<Result<(), std::io::Error>>().await
        };
        let started = std::time::Instant::now();
        assert_eq!(native_update::cancellable(operation, &cancel, "update_check_failed").await.unwrap_err(), "update_cancelled");
        assert!(started.elapsed() < Duration::from_secs(1));
    });
}

#[test]
fn install_confirmation_is_bound_to_the_exact_feed_artifact() {
    let approved = json!({"version":"1.2.3","url":"https://example.test/a","signature":"one"});
    assert!(native_update::require_same_artifact(&approved, Some(&approved)).is_ok());
    assert_eq!(native_update::require_same_artifact(&approved, None).unwrap_err(), "update_target_changed");
    for field in ["version", "url", "signature"] {
        let mut changed = approved.clone();
        changed[field] = json!("different");
        assert_eq!(native_update::require_same_artifact(&approved, Some(&changed)).unwrap_err(), "update_target_changed");
    }
}

#[test]
fn space_check_includes_cache_application_and_extraction_volumes() {
    let mut checked = Vec::new();
    let paths = [std::path::Path::new("/cache"), std::path::Path::new("/app"), std::path::Path::new("/extract")];
    assert_eq!(native_update::require_space(&paths, 100, |path| {
        checked.push(path.to_path_buf());
        Ok(if path == paths[2] { 99 } else { 100 })
    }).unwrap_err(), "update_disk_space");
    assert_eq!(checked, paths);
    assert!(native_update::require_space(&paths, 100, |_| Ok(100)).is_ok());
    assert_eq!(native_update::require_space(&paths, 100, |_| Err(std::io::Error::other("unavailable"))).unwrap_err(), "update_disk_unavailable");
}

#[cfg(unix)]
#[test]
fn extraction_volume_matches_the_updaters_same_device_selection() {
    let folder = tempfile::tempdir().unwrap();
    let executable = folder.path().join("Fixture.app/Contents/MacOS/fixture");
    std::fs::create_dir_all(executable.parent().unwrap()).unwrap();
    std::fs::write(&executable, b"fixture").unwrap();
    assert_eq!(native_update::extraction_root(&executable, folder.path()).unwrap(), folder.path());
    assert_eq!(native_update::extraction_root(&executable, &folder.path().join("missing")).unwrap(), folder.path());
}

#[test]
fn announced_package_size_bounds_preflight_and_falls_back_when_unknown() {
    assert_eq!(native_update::announced_size(&serde_json::json!({"size": 1024})), 1024);
    assert_eq!(native_update::announced_size(&serde_json::json!({"size": 0})), native_update::MAX_PACKAGE as u64);
    assert_eq!(native_update::announced_size(&serde_json::json!({})), native_update::MAX_PACKAGE as u64);
}

#[test]
fn resumed_segment_requests_only_the_missing_bytes() {
    assert_eq!(native_update::resume_offset(4096, Some(8192)), 4096);
    assert_eq!(native_update::resume_offset(0, None), 0);
    assert_eq!(native_update::resume_offset(8192, Some(8192)), 8192);
    assert_eq!(native_update::resume_offset(900, Some(800)), 0);
    assert_eq!(native_update::resume_offset(native_update::MAX_PACKAGE as u64 + 1, None), 0);
    assert_eq!(native_update::range_header(0), None);
    assert_eq!(native_update::range_header(4096).as_deref(), Some("bytes=4096-"));
}

#[cfg(unix)]
#[test]
fn identity_file_never_writes_through_links() {
    let folder = tempfile::tempdir().unwrap();
    let target = folder.path().join("outside");
    let meta = native_update::part_meta_path(folder.path());
    std::fs::write(&target, b"outside").unwrap();
    std::os::unix::fs::symlink(&target, &meta).unwrap();
    native_update::write_part_identity(&meta, "identity").unwrap();
    assert_eq!(std::fs::read(&target).unwrap(), b"outside");
    assert_eq!(std::fs::read_to_string(&meta).unwrap(), "identity");
    assert!(std::fs::symlink_metadata(&meta).unwrap().file_type().is_file());

    std::fs::remove_file(&meta).unwrap();
    std::fs::hard_link(&target, &meta).unwrap();
    native_update::write_part_identity(&meta, "second").unwrap();
    assert_eq!(std::fs::read(&target).unwrap(), b"outside");
    assert_eq!(std::fs::read_to_string(&meta).unwrap(), "second");
}

#[test]
fn empty_and_oversized_segments_restart_without_a_permanent_error() {
    let folder = tempfile::tempdir().unwrap();
    let part = native_update::part_path(folder.path());
    std::fs::write(&part, b"").unwrap();
    assert_eq!(native_update::part_bytes(&part), 0);
    drop(native_update::open_part(&part, 0).unwrap());
    assert_eq!(std::fs::metadata(&part).unwrap().len(), 0);

    std::fs::write(&part, b"0123456789").unwrap();
    assert_eq!(native_update::resume_offset(native_update::part_bytes(&part), Some(4)), 0);
    drop(native_update::open_part(&part, 0).unwrap());
    assert_eq!(std::fs::metadata(&part).unwrap().len(), 0);
}

#[test]
fn segment_identity_is_bound_to_version_and_url() {
    let folder = tempfile::tempdir().unwrap();
    let meta = native_update::part_meta_path(folder.path());
    assert_eq!(meta.file_name().unwrap(), "staged-update.part.json");
    let current = native_update::part_identity("1.2.3", "https://example.test/a");
    assert!(native_update::identity_matches(Some(&current), "1.2.3", "https://example.test/a"));
    assert!(!native_update::identity_matches(Some(&current), "1.2.4", "https://example.test/a"));
    assert!(!native_update::identity_matches(Some(&current), "1.2.3", "https://example.test/b"));
    assert!(!native_update::identity_matches(None, "1.2.3", "https://example.test/a"));
}

#[test]
fn segment_decision_follows_status_and_content_range() {
    use native_update::{parse_content_range, segment_action, ContentRange, SegmentAction};
    let resumed = parse_content_range("bytes 4096-8191/8192").unwrap();
    assert_eq!(resumed, ContentRange { start: 4096, total: Some(8192) });
    assert_eq!(parse_content_range("bytes 0-99/*").unwrap().total, None);
    assert_eq!(parse_content_range("attachment"), None);
    assert_eq!(segment_action(4096, 206, Some(&resumed)), SegmentAction::Append);
    // A 206 that answers a different start must not be appended.
    assert_eq!(segment_action(4096, 206, Some(&ContentRange { start: 0, total: Some(8192) })),
               SegmentAction::Restart);
    // The server ignored Range: truncate and download from zero.
    assert_eq!(segment_action(4096, 200, None), SegmentAction::Restart);
    assert_eq!(segment_action(0, 200, None), SegmentAction::Restart);
    // An unsatisfiable range and any other status invalidate the segment.
    assert_eq!(segment_action(4096, 416, None), SegmentAction::Rejected);
    assert_eq!(segment_action(0, 500, None), SegmentAction::Rejected);
}

#[test]
fn untrusted_segments_are_discarded_and_never_resumed() {
    let folder = tempfile::tempdir().unwrap();
    let part = native_update::part_path(folder.path());
    assert_eq!(part.file_name().unwrap(), "staged-update.part");
    assert_eq!(native_update::cached_path(folder.path()).file_name().unwrap(), "staged-update.bin");
    std::fs::write(&part, b"partial").unwrap();
    assert_eq!(native_update::part_bytes(&part), 7);
    // Resuming appends after the persisted bytes instead of overwriting them.
    {
        use std::io::Write;
        let mut file = native_update::open_part(&part, 7).unwrap();
        file.write_all(b"more").unwrap();
        file.sync_all().unwrap();
    }
    assert_eq!(std::fs::read(&part).unwrap(), b"partialmore");
    assert!(native_update::discards_segment("update_download_or_signature_failed"));
    assert!(native_update::discards_segment("update_range_rejected"));
    assert!(native_update::discards_segment("update_size_limit"));
    assert!(!native_update::discards_segment("update_cancelled"));
    native_update::discard_part(&part);
    assert!(!part.exists());
    // A non-file entry is never treated as resumable state.
    std::fs::create_dir(&part).unwrap();
    assert_eq!(native_update::part_bytes(&part), 0);
    assert!(!part.exists());
    #[cfg(unix)] {
        std::fs::write(folder.path().join("target"), b"outside").unwrap();
        std::os::unix::fs::symlink(folder.path().join("target"), &part).unwrap();
        assert_eq!(native_update::part_bytes(&part), 0);
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        assert!(!part.exists());
        std::fs::hard_link(folder.path().join("target"), &part).unwrap();
        assert_eq!(native_update::part_bytes(&part), 0);
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        assert!(!part.exists());
        // Direct open must not follow or append through either link type.
        std::os::unix::fs::symlink(folder.path().join("target"), &part).unwrap();
        {
            use std::io::Write;
            let mut file = native_update::open_part(&part, 0).unwrap();
            file.write_all(b"new").unwrap();
        }
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        assert_eq!(std::fs::read(&part).unwrap(), b"new");
        native_update::discard_part(&part);
        std::fs::hard_link(folder.path().join("target"), &part).unwrap();
        {
            use std::io::Write;
            let mut file = native_update::open_part(&part, 0).unwrap();
            file.write_all(b"new").unwrap();
        }
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        native_update::discard_part(&part);
    }
    // A stale empty segment must not make exclusive creation fail forever.
    std::fs::write(&part, b"").unwrap();
    {
        use std::io::Write;
        let mut file = native_update::open_part(&part, 0).unwrap();
        file.write_all(b"fresh").unwrap();
    }
    assert_eq!(std::fs::read(&part).unwrap(), b"fresh");
    native_update::discard_part(&part);
    // A segment whose length changed under the recorded offset is not appended.
    std::fs::write(&part, b"123").unwrap();
    assert_eq!(native_update::open_part(&part, 4).unwrap_err(), "update_range_rejected");
    assert!(!part.exists());
}

#[test]
fn asset_target_is_preflighted_before_download() {
    use native_update::{detect_target, require_compatible_asset, Arch, Platform};
    assert_eq!(detect_target("desktop_1.2.3_amd64_linux"),
               (Some(Platform::Linux), Some(Arch::X86_64)));
    assert_eq!(detect_target("desktop-1.2.3-aarch64-apple-darwin.tar.gz"),
               (Some(Platform::Macos), Some(Arch::Aarch64)));
    let mac_arm = (Some(Platform::Macos), Some(Arch::Aarch64));
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-aarch64-apple-darwin.tar.gz",
        None, mac_arm).is_ok());
    assert_eq!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-x86_64-pc-windows-msvc.zip",
        None, mac_arm).unwrap_err(), "update_incompatible_platform");
    assert_eq!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-arm64-apple-darwin.zip",
        None, (Some(Platform::Macos), Some(Arch::X86_64))).unwrap_err(), "update_incompatible_platform");
    // The asset name can carry the marker when the URL does not.
    assert_eq!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/package.zip",
        Some("desktop-1.2.3-x86_64-unknown-linux-gnu.zip"),
        mac_arm).unwrap_err(), "update_incompatible_platform");
    // A universal/multi-architecture label is ambiguous, so it is not rejected.
    assert_eq!(detect_target("desktop-1.2.3-macos-arm64-x86_64.zip"),
               (Some(Platform::Macos), None));
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-macos-arm64-x86_64.zip",
        None, mac_arm).is_ok());
    // Unlabelled assets and an unrecognised host stay eligible.
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-latest.zip", None, mac_arm).is_ok());
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-x86_64-pc-windows-msvc.zip",
        None, (None, None)).is_ok());
}

#[test]
fn snapshot_reports_host_target_and_explicit_unknowns() {
    let mut downloading = serde_json::json!({
        "state":"downloading","stage":"downloading","bytes":512,"total":1024
    });
    native_update::describe_snapshot(&mut downloading, "0.5.127", "macos", "aarch64");
    assert_eq!(downloading["current_version"], serde_json::json!("0.5.127"));
    assert_eq!(downloading["platform"], serde_json::json!("macos"));
    assert_eq!(downloading["arch"], serde_json::json!("aarch64"));
    assert_eq!(downloading["bytes"], serde_json::json!(512));
    assert_eq!(downloading["total"], serde_json::json!(1024));
    assert_eq!(downloading["bytes_per_second"], Value::Null);
    let mut idle = serde_json::json!({"state":"idle"});
    native_update::describe_snapshot(&mut idle, "1.0.0", "windows", "x86_64");
    for key in ["stage", "bytes", "total", "bytes_per_second"] {
        assert_eq!(idle[key], Value::Null, "{key} must be explicit for the panel");
    }
    assert_eq!(native_update::host_platform(), std::env::consts::OS);
    assert_eq!(native_update::host_arch(), std::env::consts::ARCH);
}

#[test]
fn progress_is_throttled_and_rates_are_derived() {
    let start = std::time::Instant::now();
    let mut progress = native_update::Progress::new(start, 1000);
    assert!(!progress.due(start + Duration::from_millis(100)));
    assert!(progress.due(start + Duration::from_millis(250)));
    assert!(!progress.due(start + Duration::from_millis(499)));
    assert!(progress.due(start + Duration::from_millis(500)));
    assert_eq!(native_update::PROGRESS_INTERVAL, Duration::from_millis(250));
    // Only bytes received in this session count towards the rate.
    assert_eq!(progress.rate(3000, start + Duration::from_secs(2)), Some(1000));
    assert_eq!(native_update::bytes_per_second(1000, Duration::from_secs(2)), Some(500));
    assert_eq!(native_update::bytes_per_second(0, Duration::from_secs(2)), None);
    assert_eq!(native_update::bytes_per_second(1000, Duration::ZERO), None);
}

#[test]
fn transfer_deadlines_keep_their_previous_bounds() {
    assert_eq!(native_update::UPDATE_CONNECT_TIMEOUT, Duration::from_secs(15));
    assert_eq!(native_update::UPDATE_RESPONSE_TIMEOUT, Duration::from_secs(30));
    assert_eq!(native_update::UPDATE_IDLE_TIMEOUT, Duration::from_secs(30));
    assert_eq!(native_update::UPDATE_TOTAL_TIMEOUT, Duration::from_secs(600));
    assert_eq!(native_update::MAX_PACKAGE, 512 * 1024 * 1024);
}

#[test]
fn authorization_revisions_only_move_forward_and_are_validated() {
    use native_update::{Authorization, NativeUpdater};
    assert_eq!(Authorization::parse(1, "sometimes", "patch", "any").unwrap_err(), "update_authorization_invalid");
    assert_eq!(Authorization::parse(1, "download", "major", "any").unwrap_err(), "update_authorization_invalid");
    assert_eq!(Authorization::parse(1, "download", "patch", "lunch").unwrap_err(), "update_authorization_invalid");
    let updater = NativeUpdater::default();
    assert_eq!(updater.authorization().revision, 0);
    assert!(updater.authorize(Authorization::parse(1, "automatic", "minor", "night").unwrap()).unwrap());
    assert_eq!(updater.authorization().window, "night");
    // 重复或倒退的版本号来自并发/过期的前端调用，不能回滚已生效的授权。
    assert!(!updater.authorize(Authorization::parse(1, "manual", "patch", "any").unwrap()).unwrap());
    assert!(!updater.authorize(Authorization::parse(0, "manual", "patch", "any").unwrap()).unwrap());
    assert_eq!(updater.authorization().policy, "automatic");
    assert_eq!(updater.authorization().revision, 1);
}

#[test]
fn superseding_a_waiting_install_revokes_it_without_cancelling_downloads() {
    use native_update::{Authorization, NativeUpdater};
    let updater = NativeUpdater::default();
    updater.authorize(Authorization::parse(1, "automatic", "patch", "any").unwrap()).unwrap();
    let waiting = updater.begin_waiting(1);
    assert!(updater.require_authorization(1).is_ok());
    assert!(updater.authorize(Authorization::parse(2, "download", "patch", "any").unwrap()).unwrap());
    assert_eq!(updater.require_authorization(1).unwrap_err(), "update_authorization_changed");
    // 撤销只针对旧的等待安装：正在进行的下载不因策略变化被中断。
    assert!(!updater.cancelled());
    drop(waiting);
    let operation = updater.begin().unwrap();
    assert!(updater.require_authorization(2).is_ok());
    drop(operation);
}

#[test]
fn install_is_bound_to_the_revision_it_was_approved_under() {
    use native_update::{Authorization, NativeUpdater};
    let updater = NativeUpdater::default();
    updater.authorize(Authorization::parse(4, "download", "patch", "any").unwrap()).unwrap();
    assert!(updater.require_authorization(4).is_ok());
    assert_eq!(updater.require_authorization(3).unwrap_err(), "update_authorization_changed");
    // 新操作开始时清除上一次撤销标记，但不会放宽版本比对。
    let operation = updater.begin().unwrap();
    updater.authorize(Authorization::parse(5, "automatic", "patch", "weekend").unwrap()).unwrap();
    assert_eq!(updater.require_authorization(4).unwrap_err(), "update_authorization_changed");
    assert!(updater.require_authorization(5).is_ok());
    drop(operation);
}

/// The install contract comes from the stored authorization, never from the
/// caller: a manual click may run in any window, an automatic install may not.
#[test]
fn install_needs_an_approved_revision_and_never_borrows_the_stored_one() {
    use native_update::required_revision;
    assert_eq!(required_revision(None).unwrap_err(), "update_authorization_invalid");
    // Revision 0 is the "never authorized" default, so it is not an approval.
    assert_eq!(required_revision(Some(0)).unwrap_err(), "update_authorization_invalid");
    assert_eq!(required_revision(Some(3)).unwrap(), 3);
}

#[test]
fn version_scope_admits_only_its_own_boundary() {
    use native_update::scope_allows;
    assert!(scope_allows("patch", "1.2.3", "1.2.4"));
    assert!(!scope_allows("patch", "1.2.3", "1.3.0"));
    assert!(scope_allows("minor", "1.2.3", "1.3.0"));
    assert!(!scope_allows("minor", "1.2.3", "2.0.0"));
    assert!(scope_allows("stable", "1.2.3", "2.0.0"));
    // An equal, older or unparsable target is never an upgrade.
    for target in ["1.2.3", "1.2.2", "1.2.4-beta", "bad"] {
        assert!(!scope_allows("stable", "1.2.3", target), "{target}");
    }
    assert!(!scope_allows("major", "1.2.3", "2.0.0"));
}

#[test]
fn automatic_install_uses_the_stored_policy_window_and_scope() {
    use native_update::{install_permit, Authorization, InstallRequest};
    let stored = Authorization::parse(3, "automatic", "patch", "night").unwrap();
    let automatic = InstallRequest { revision: Some(3), automatic: true, window: "night",
        current_version: "1.2.3", target_version: "1.2.4" };
    let permit = install_permit(&stored, &automatic).unwrap();
    assert_eq!((permit.revision, permit.window.as_str()), (3, "night"));
    // The stored policy must really be automatic.
    for policy in ["manual", "download"] {
        let stored = Authorization::parse(3, policy, "patch", "night").unwrap();
        assert_eq!(install_permit(&stored, &automatic).unwrap_err(), "update_authorization_changed");
    }
    // The caller may not present a window the user never stored.
    let widened = InstallRequest { window: "any", ..automatic };
    let narrowed = InstallRequest { window: "weekend", ..automatic };
    assert_eq!(install_permit(&stored, &widened).unwrap_err(), "update_authorization_changed");
    assert_eq!(install_permit(&stored, &narrowed).unwrap_err(), "update_authorization_changed");
    // The staged target must sit inside the stored scope.
    let minor = InstallRequest { target_version: "1.3.0", ..automatic };
    assert_eq!(install_permit(&stored, &minor).unwrap_err(), "update_target_changed");
    let stale = InstallRequest { revision: Some(2), ..automatic };
    assert_eq!(install_permit(&stored, &stale).unwrap_err(), "update_authorization_changed");
    // A stored minor/stable scope does admit the matching target.
    let minor_stored = Authorization::parse(3, "automatic", "minor", "night").unwrap();
    assert_eq!(install_permit(&minor_stored, &minor).unwrap().window, "night");
}

#[test]
fn manual_install_overrides_the_window_but_still_needs_the_revision() {
    use native_update::{install_permit, Authorization, InstallRequest};
    let stored = Authorization::parse(4, "download", "patch", "weekend").unwrap();
    let manual = InstallRequest { revision: Some(4), automatic: false, window: "any",
        current_version: "1.2.3", target_version: "2.0.0" };
    // An explicit click replaces now, outside the stored window and scope.
    assert_eq!(install_permit(&stored, &manual).unwrap().window, "any");
    let missing = InstallRequest { revision: None, ..manual };
    assert_eq!(install_permit(&stored, &missing).unwrap_err(), "update_authorization_invalid");
    let stale = InstallRequest { revision: Some(3), ..manual };
    assert_eq!(install_permit(&stored, &stale).unwrap_err(), "update_authorization_changed");
}

#[test]
fn changing_the_stored_authorization_stops_a_waiting_automatic_replacement() {
    use native_update::{Authorization, InstallRequest, NativeUpdater};
    let updater = NativeUpdater::default();
    updater.authorize(Authorization::parse(1, "automatic", "patch", "night").unwrap()).unwrap();
    let request = InstallRequest { revision: Some(1), automatic: true, window: "night",
        current_version: "1.2.3", target_version: "1.2.4" };
    assert_eq!(updater.require_install_permit(&request).unwrap().window, "night");
    // Even a window-only change revokes: the stored window no longer matches.
    updater.authorize(Authorization::parse(2, "automatic", "patch", "weekend").unwrap()).unwrap();
    assert_eq!(updater.require_install_permit(&request).unwrap_err(), "update_authorization_changed");
}
use super::*;
use std::io::BufReader;

fn proxy_request(state: &RuntimeState, path: &str, body: Option<Value>) -> Result<Reply, String> {
    tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap()
        .block_on(transport::request(state, path, body, "test-request".into()))
}


#[test]
fn async_proxy_cancels_real_blocked_transport_and_releases_request_slot() {
    for send_headers in [false, true] {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (entered_tx, entered_rx) = std::sync::mpsc::channel();
        let (closed_tx, closed_rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" { break; }
            }
            if send_headers {
                stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n").unwrap();
            }
            entered_tx.send(()).unwrap();
            let mut byte = [0];
            assert_eq!(stream.read(&mut byte).unwrap(), 0, "cancelled transport remained open");
            closed_tx.send(()).unwrap();
        });
        let state = std::sync::Arc::new(RuntimeState::default());
        let (client, transport) = transport::clients().unwrap();
        *state.target.lock().unwrap() = Some(Connection { client, transport, origin: origin.clone(),
            csrf_origin: origin, cookie: None, csrf: "fixture".into(), name: "fixture".into(), owned: false });
        let cancel_state = state.clone();
        let canceller = std::thread::spawn(move || {
            entered_rx.recv_timeout(Duration::from_secs(3)).unwrap();
            cancel_state.transport.cancel("cancel-test");
        });
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let began = std::time::Instant::now();
        let result = runtime.block_on(async {
            let result = transport::request(&state, "state", None, "cancel-test".into()).await;
            assert_eq!(state.transport.active(), 0);
            tokio::time::timeout(Duration::from_secs(1), async {
                while closed_rx.try_recv().is_err() { tokio::time::sleep(Duration::from_millis(5)).await; }
            }).await.unwrap();
            result
        });
        assert_eq!(result.err().unwrap(), "Request cancelled");
        assert!(began.elapsed() < Duration::from_secs(2));
        canceller.join().unwrap();
        worker.join().unwrap();
    }
}

fn release() -> Value {
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x86_64",
        _ => "arm64",
    };
    json!({
        "tag_name": "v1.2.3", "body": "notes", "assets": [
            {"name": format!("adaptive-rsi-1.2.3-macos-{}-adhoc.dmg", arch), "browser_download_url": "https://example.test/update.dmg"},
            {"name": format!("adaptive-rsi-1.2.3-macos-{}-adhoc.dmg.sha256", arch), "browser_download_url": "https://example.test/update.sha256"}
        ]
    })
}

#[test]
fn release_check_distinguishes_invalid_metadata_from_no_update() {
    let fixture = release();
    assert!(desktop_release(&fixture, "1.2.2").unwrap().is_some());
    for current in ["1.2.3", "1.2.4", "2.0.0"] {
        assert!(desktop_release(&fixture, current).unwrap().is_none());
    }
    for version in [
        "invalid",
        "1.2",
        "01.2.3",
        "+1.2.3",
        "vv1.2.3",
        "1.2.3.4",
        "1.2.3-beta",
    ] {
        let mut broken = fixture.clone();
        broken["tag_name"] = json!(version);
        assert!(desktop_release(&broken, "1.0.0").is_err(), "{version}");
    }
    assert!(desktop_release(&fixture, "invalid").is_err());
    for field in ["tag_name", "assets"] {
        let mut broken = fixture.clone();
        broken.as_object_mut().unwrap().remove(field);
        assert!(desktop_release(&broken, "1.0.0").is_err(), "{field}");
        if field == "tag_name" {
            assert!(desktop_release(&broken, "1.2.3").is_err());
        } else {
            assert!(desktop_release(&broken, "1.2.3").unwrap().is_none());
            assert!(desktop_release(&broken, "1.2.4").unwrap().is_none());
        }
    }
    for index in [0, 1] {
        let mut broken = fixture.clone();
        broken["assets"].as_array_mut().unwrap().remove(index);
        assert!(desktop_release(&broken, "1.0.0").is_err());
        for url in ["", "garbage", "http://example.test/file"] {
            let mut broken = fixture.clone();
            broken["assets"][index]["browser_download_url"] = json!(url);
            assert!(desktop_release(&broken, "1.0.0").is_err());
        }
    }
}

#[test]
fn session_rotation_refreshes_cookie_and_csrf_for_local_and_remote_connections() {
    for explicit_cookie in [true, false] {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            for step in 0..4 {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut headers = String::new();
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    assert!(!line.is_empty());
                    headers.push_str(&line.to_lowercase());
                }
                if step > 0 {
                    let token = if step == 1 { "old" } else { "new" };
                    assert!(
                        headers.contains(&format!("cookie: rsi_123={token}\r\n")),
                        "{headers}"
                    );
                }
                if step == 1 || step == 2 {
                    let csrf = if step == 1 { "old-csrf" } else { "new-csrf" };
                    assert!(
                        headers.contains(&format!("x-rsi-csrf: {csrf}\r\n")),
                        "{headers}"
                    );
                    assert!(headers.contains("origin: https://public.example.test\r\n"));
                }
                let (cookie, body) = match step {
                    0 => ("Set-Cookie: rsi_123=old; Path=/; HttpOnly\r\n", "{}"),
                    1 => ("Set-Cookie: rsi_123=new; Path=/; HttpOnly\r\n", "{\"csrf\":\"new-csrf\",\"url\":\"https://public.example.test/?token=new\"}"),
                    _ => ("", "{}"),
                };
                write!(stream, "HTTP/1.1 200 OK\r\n{cookie}Content-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            }
        });
        let (client, transport) = transport::clients().unwrap();
        let connection = Connection {
            client,
            transport,
            origin,
            csrf_origin: "https://public.example.test".into(),
            cookie: explicit_cookie.then(|| "rsi_123=old".into()),
            csrf: "old-csrf".into(),
            name: "fixture".into(),
            owned: false,
        };
        call(&connection, "health", None).unwrap();
        let state = RuntimeState::default();
        *state.target.lock().unwrap() = Some(connection.clone());
        *state.local.lock().unwrap() = Some(connection);
        assert_eq!(
            proxy_request(
                &state,
                "session/rotate?fixture=1",
                Some(json!({"confirm": true}))
            )
            .unwrap()
            .status,
            200
        );
        assert_eq!(
            proxy_request(&state, "config", Some(json!({})))
                .unwrap()
                .status,
            200
        );
        // 切回本机连接后也必须使用新凭据。
        *state.target.lock().unwrap() = state.local.lock().unwrap().clone();
        assert_eq!(proxy_request(&state, "health", None).unwrap().status, 200);
        let local = state.local.lock().unwrap();
        let local = local.as_ref().unwrap();
        assert_eq!(local.cookie.as_deref(), Some("rsi_123=new"));
        assert_eq!(local.csrf, "new-csrf");
        worker.join().unwrap();
    }
}

fn update_state(downloading: bool, percent: u64) -> DesktopUpdate {
    DesktopUpdate {
        version: "9.9.9".into(),
        notes: String::new(),
        url: "https://example.test/update.dmg".into(),
        sha_url: "https://example.test/update.sha256".into(),
        error: String::new(),
        error_code: String::new(),
        percent: AtomicU64::new(percent),
        downloading: AtomicBool::new(downloading),
        opened: AtomicBool::new(false),
    }
}

#[cfg(target_os = "macos")]
const LEGACY_DMG_FIXTURE: &[u8] = b"fixture DMG";
#[cfg(target_os = "macos")]
const LEGACY_DMG_SHA256: &str = "eac5da462cb16554774d722fa4093739ffc9ade2f4c6e8ee54041e71908f1408";

#[cfg(target_os = "macos")]
#[test]
fn legacy_dmg_failed_open_can_be_retried_with_or_without_an_existing_image() {
    for already_published in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("update.dmg");
        if already_published {
            fs::write(&path, LEGACY_DMG_FIXTURE).unwrap();
        }
        let state = RuntimeState::default();
        *state.update.lock().unwrap() = Some(update_state(false, 0));
        let gate = Mutex::new(());
        let mut downloads = 0;
        for opens in [false, true] {
            let (_, _, _, _gate) = begin_desktop_download(&state, &gate).unwrap();
            assert!(state.update.lock().unwrap().as_ref().unwrap().error.is_empty());
            let mut staged_path = None;
            let result = stage_legacy_dmg(
                &path,
                LEGACY_DMG_SHA256,
                |file| {
                    downloads += 1;
                    file.write_all(LEGACY_DMG_FIXTURE).unwrap();
                    Ok(())
                },
                |staged| {
                    assert_ne!(staged, path);
                    assert_eq!(fs::read(staged).unwrap(), LEGACY_DMG_FIXTURE);
                    assert_eq!(path.exists(), already_published);
                    staged_path = Some(staged.to_path_buf());
                    opens
                },
            );
            assert!(!staged_path.unwrap().parent().unwrap().exists());
            complete_desktop_download(&state, result);
            let guard = state.update.lock().unwrap();
            let update = guard.as_ref().unwrap();
            assert!(!update.downloading.load(Ordering::Relaxed));
            assert_eq!(update.opened.load(Ordering::Relaxed), opens);
            assert_eq!(update.error, if opens { "" } else { "Cannot open installer" });
            if opens || already_published {
                assert_eq!(fs::read(&path).unwrap(), LEGACY_DMG_FIXTURE);
            } else {
                assert!(!path.exists());
            }
        }
        assert_eq!(downloads, if already_published { 0 } else { 2 });
    }
}

#[cfg(target_os = "macos")]
#[test]
fn legacy_dmg_does_not_replace_a_file_published_during_attach() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("update.dmg");
    let result = stage_legacy_dmg(
        &path,
        LEGACY_DMG_SHA256,
        |file| {
            file.write_all(LEGACY_DMG_FIXTURE).unwrap();
            Ok(())
        },
        |_| {
            fs::write(&path, b"another writer's image").unwrap();
            true
        },
    );
    assert_eq!(result, Err(("A download with this name already exists", "")));
    assert_eq!(fs::read(path).unwrap(), b"another writer's image");
}

#[test]
fn force_recheck_never_replaces_an_inflight_download() {
    let mut update = update_state(true, 4200);
    // 下载中的状态无论是否 force 都必须保留给轮询：替换会把 downloading 清零，
    // 让前端误判可重新下载，并使双下载防护失去依据。
    for force in [false, true] {
        assert!(should_preserve_update(&update, force), "force={force}");
    }
    // 下载已结束（成功打开/失败）时，常规轮询保留状态，显式 force 才允许重新检查。
    update.downloading.store(false, Ordering::Relaxed);
    update.opened.store(true, Ordering::Relaxed);
    assert!(should_preserve_update(&update, false));
    assert!(!should_preserve_update(&update, true));
    update.opened.store(false, Ordering::Relaxed);
    update.error = "Checksum verification failed".into();
    assert!(should_preserve_update(&update, false));
    assert!(!should_preserve_update(&update, true));
    // 没有更新的普通状态不保留：每次检查都应重新请求发布信息。
    assert!(!should_preserve_update(&update_state(false, 0), false));
}

#[test]
fn a_second_download_is_refused_even_if_the_flag_was_cleared() {
    let state = RuntimeState::default();
    let gate = Mutex::new(());
    *state.update.lock().unwrap() = Some(update_state(false, 6400));
    let (url, sha_url, version, gate_guard) =
        begin_desktop_download(&state, &gate).expect("first download starts");
    assert_eq!(
        (url.as_str(), sha_url.as_str(), version.as_str()),
        (
            "https://example.test/update.dmg",
            "https://example.test/update.sha256",
            "9.9.9"
        )
    );
    assert!(state
        .update
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .downloading
        .load(Ordering::Relaxed));
    // 模拟下载推进与并发状态替换：进度已更新、downloading 标志被清零。
    {
        let update = state.update.lock().unwrap();
        let update = update.as_ref().unwrap();
        update.downloading.store(false, Ordering::Relaxed);
        update.percent.store(3200, Ordering::Relaxed);
    }
    assert!(
        begin_desktop_download(&state, &gate).is_err(),
        "a second writer must be refused while the gate is held"
    );
    assert_eq!(
        state
            .update
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .percent
            .load(Ordering::Relaxed),
        3200,
        "refused download must not reset progress"
    );
    drop(gate_guard);
    // 闸门释放后才能再次开始下载。
    assert!(begin_desktop_download(&state, &gate).is_ok());
}

#[test]
fn download_refused_without_update_state_or_while_flagged_downloading() {
    let state = RuntimeState::default();
    let gate = Mutex::new(());
    assert!(begin_desktop_download(&state, &gate).is_err());
    *state.update.lock().unwrap() = Some(update_state(true, 1200));
    assert!(begin_desktop_download(&state, &gate).is_err());
    assert_eq!(
        state
            .update
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .percent
            .load(Ordering::Relaxed),
        1200,
        "refused download must not reset progress"
    );
}

#[cfg(unix)]
#[test]
fn child_wait_deadline_does_not_wait_for_process_exit() {
    let mut child = Command::new("sleep").arg("30").spawn().unwrap();
    let start = std::time::Instant::now();
    assert!(!wait_for_child(&mut child, Duration::from_millis(20)));
    assert!(start.elapsed() < Duration::from_secs(1));
    stop_child(&mut child, Duration::ZERO);
    assert!(child.try_wait().unwrap().is_some());
}

#[test]
fn private_handshake_stream_keeps_rotations_and_partial_lines() {
    let receiver = handshake_stream(io::Cursor::new(b"first\nrotated\n"));
    assert_eq!(receiver.recv().unwrap().unwrap(), "first\n");
    assert_eq!(receiver.recv().unwrap().unwrap(), "rotated\n");
    assert!(receiver.recv().is_err());

    // The initial read ends inside the second line after consuming the first.
    let longest = format!("{}\n", "x".repeat(65535));
    let receiver = handshake_stream(io::Cursor::new(format!("first\n{longest}rotated\nlast")));
    for line in ["first\n", &longest, "rotated\n", "last"] {
        assert_eq!(receiver.recv().unwrap().unwrap(), line);
    }
    assert!(receiver.recv().is_err());
}

#[test]
fn private_handshake_stream_reads_at_most_65537_bytes_of_an_oversized_line() {
    use std::sync::{mpsc, Arc};
    use std::sync::atomic::AtomicUsize;

    struct NoNewline {
        remaining: usize,
        bytes_read: Arc<AtomicUsize>,
        wait_for_eof: mpsc::Receiver<()>,
    }
    impl Read for NoNewline {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            if self.remaining == 0 {
                let _ = self.wait_for_eof.recv_timeout(Duration::from_secs(3));
                return Ok(0);
            }
            let count = self.remaining.min(buffer.len()).min(8192);
            buffer[..count].fill(b'x');
            self.remaining -= count;
            self.bytes_read.fetch_add(count, Ordering::Relaxed);
            Ok(count)
        }
    }

    for available in [1024 * 1024 + 8, 65537] {
        let (_keep_open, wait_for_eof) = mpsc::channel();
        let bytes_read = Arc::new(AtomicUsize::new(0));
        let receiver = handshake_stream(NoNewline { remaining: available, bytes_read: bytes_read.clone(), wait_for_eof });
        let error = receiver.recv_timeout(Duration::from_secs(1)).unwrap().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert_eq!(bytes_read.load(Ordering::Relaxed), 65537);
        assert!(receiver.recv().is_err());
    }

    let receiver = handshake_stream(io::Cursor::new(format!("{}\n", "x".repeat(65536))));
    assert_eq!(receiver.recv().unwrap().unwrap_err().kind(), io::ErrorKind::InvalidData);
}

#[test]
fn private_rotation_is_bound_to_generation_and_service_identity() {
    let state = RuntimeState::default();
    let (client, transport) = transport::clients().unwrap();
    let connection = Connection { client, transport, origin: "http://127.0.0.1:4321".into(),
        csrf_origin: "https://public.example.test".into(), cookie: Some("rsi_fixture=old".into()),
        csrf: "old".into(), name: "fixture".into(), owned: false };
    *state.local.lock().unwrap() = Some(connection.clone());
    *state.target.lock().unwrap() = Some(connection);
    let fixture = json!({"origin":"https://public.example.test","local_origin":"http://127.0.0.1:4321",
        "cookie":"rsi_fixture","token":"new","csrf":"new","owned":false,"pid":123,"rotated":true});
    for (field, value) in [("pid", json!(124)), ("local_origin", json!("http://127.0.0.1:4322")),
                            ("cookie", json!("other")), ("owned", json!(true))] {
        let mut invalid = fixture.clone(); invalid[field] = value;
        assert!(!publish_runtime_session(&state, 0, 123, serde_json::from_value(invalid).unwrap()));
    }
    assert!(!publish_runtime_session(&state, 1, 123, serde_json::from_value(fixture.clone()).unwrap()));
    // The serving PID is distinct from the child used to supervise the service.
    state.pid.store(9000, Ordering::SeqCst);
    assert!(publish_runtime_session(&state, 0, 123, serde_json::from_value(fixture).unwrap()));
    assert_eq!(state.local.lock().unwrap().as_ref().unwrap().csrf, "new");
    assert_eq!(state.target.lock().unwrap().as_ref().unwrap().cookie.as_deref(), Some("rsi_fixture=new"));
}

#[cfg(unix)]
#[test]
fn shutdown_timeout_invalidates_connection_until_actual_exit_recovery() {
    let state = RuntimeState::default();
    let (client, transport) = transport::clients().unwrap();
    let connection = Connection { client, transport, origin: "http://127.0.0.1:4321".into(),
        csrf_origin: "http://127.0.0.1:4321".into(), cookie: None, csrf: "fixture".into(),
        name: "fixture".into(), owned: true };
    *state.local.lock().unwrap() = Some(connection.clone());
    *state.target.lock().unwrap() = Some(connection);
    let child = Command::new("python3").args(["-c", "import sys,time; sys.stdin.read(); time.sleep(0.2)"])
        .stdin(Stdio::piped()).spawn().unwrap();
    *state.child.lock().unwrap() = Some(child);
    let exiting = shutdown_runtime(&state, Duration::from_millis(10)).expect("still exiting");
    assert!(state.child.lock().unwrap().is_none());
    assert!(state.local.lock().unwrap().is_none());
    assert!(state.target.lock().unwrap().is_none());
    assert_eq!(state.connection_generation.load(Ordering::SeqCst), 1);
    assert!(state.recovering.load(Ordering::SeqCst));
    assert!(exiting.stdin.is_none());
    *state.child.lock().unwrap() = Some(exiting);
    let _start = state.local_start_gate.lock().unwrap();
    assert!(wait_for_child(state.child.lock().unwrap().as_mut().unwrap(), Duration::from_secs(3)));
    assert!(reap_update_runtime(&state));
    assert!(state.child.lock().unwrap().is_none());
}

#[cfg(unix)]
#[test]
fn update_recovery_restores_local_without_replacing_selected_remote() {
    let state = RuntimeState::default();
    let connection = |origin: &str, owned| {
        let (client, transport) = transport::clients().unwrap();
        Connection { client, transport, origin: origin.into(), csrf_origin: origin.into(),
            cookie: None, csrf: "fixture".into(), name: "fixture".into(), owned }
    };
    let remote = connection("https://remote.invalid", false);
    let remote_meta = json!({"remote": true});
    let old = connection("http://127.0.0.1:4321", true);
    *state.target.lock().unwrap() = Some(remote.clone());
    *state.remote_meta.lock().unwrap() = remote_meta.clone();
    *state.local.lock().unwrap() = Some(old);
    *state.child.lock().unwrap() = Some(Command::new("python3").args(["-c", "import sys; sys.stdin.read()"])
        .stdin(Stdio::piped()).spawn().unwrap());

    let _start = state.local_start_gate.lock().unwrap();
    let mut stopped = shutdown_runtime(&state, Duration::from_secs(2)).unwrap();
    assert!(stopped.try_wait().unwrap().is_some());
    assert!(state.recovering.load(Ordering::SeqCst));
    assert!(state.local.lock().unwrap().is_none());
    assert!(state.target.lock().unwrap().as_ref().unwrap().transport.same_connection(&remote.transport));
    assert_eq!(*state.remote_meta.lock().unwrap(), remote_meta);

    let fresh = connection("http://127.0.0.1:4322", true);
    let child = Command::new("python3").args(["-c", "pass"]).spawn().unwrap();
    let pid = child.id();
    assert_eq!(publish_local_runtime(&state, child, fresh.clone(), true), 1);
    assert_eq!(state.pid.load(Ordering::SeqCst), pid);
    assert!(!state.recovering.load(Ordering::SeqCst));
    assert!(state.local.lock().unwrap().as_ref().unwrap().transport.same_connection(&fresh.transport));
    assert!(state.target.lock().unwrap().as_ref().unwrap().transport.same_connection(&remote.transport));
    assert_eq!(*state.remote_meta.lock().unwrap(), remote_meta);
    assert_eq!(state.connection_generation.load(Ordering::SeqCst), 0);
    state.child.lock().unwrap().take().unwrap().wait().unwrap();
}

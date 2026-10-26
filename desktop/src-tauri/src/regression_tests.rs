use super::*;

fn release() -> Value {
    json!({
        "tag_name": "v1.2.3", "body": "notes", "assets": [
            {"name": "adaptive-rsi-1.2.3-macos-arm64-adhoc.dmg", "browser_download_url": "https://example.test/update.dmg"},
            {"name": "adaptive-rsi-1.2.3-macos-arm64-adhoc.dmg.sha256", "browser_download_url": "https://example.test/update.sha256"}
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
        assert!(desktop_release(&broken, "1.2.3").is_err(), "{field}");
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
        let connection = Connection {
            client: client().unwrap(),
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

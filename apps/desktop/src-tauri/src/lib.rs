#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_fs::init())
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_opener::init())
    .plugin(tauri_plugin_dialog::init())
    .plugin(tauri_plugin_process::init())
    .plugin(tauri_plugin_clipboard_manager::init())
    .plugin(tauri_plugin_http::init())
    .invoke_handler(tauri::generate_handler![capture_screen])
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while building tauri application");
}

#[tauri::command]
async fn capture_screen(target: Option<String>, delay_seconds: Option<u32>) -> Result<serde_json::Value, String> {
    // Call the core's screen.capture tool via HTTP
    let client = reqwest::Client::new();
    let params = serde_json::json!({
        "target": target.unwrap_or_else(|| "screen".to_string()),
        "delay_seconds": delay_seconds.unwrap_or(0),
    });

    // Core address should be configurable; default to localhost for dev
    let core_addr = std::env::var("NEXUS_CORE_ADDR").unwrap_or_else(|_| "127.0.0.1:8787".to_string());
    let url = format!("http://{}/api/tools/execute", core_addr);

    let response = client
        .post(&url)
        .json(&serde_json::json!({
            "name": "screen.capture",
            "params": params,
            "conversation_id": null,
        }))
        .timeout(std::time::Duration::from_secs(30))
        .send()
        .await
        .map_err(|e| format!("Failed to call screen.capture: {}", e))?;

    let result: serde_json::Value = response
        .json()
        .await
        .map_err(|e| format!("Failed to parse screen.capture response: {}", e))?;

    Ok(result)
}
const elements = Object.fromEntries(["connect", "start", "stop", "export", "status", "limit"].map(name => [name, document.getElementById(name)]));
let device;
let records = [];
let active = false;
let started = 0;
let metadata;
let limit = 500000;
let reason = "manual";

function stopCapture(stopReason = "manual") {
  active = false;
  reason = stopReason;
  elements.stop.disabled = true;
  elements.start.disabled = !device?.opened;
  elements.export.disabled = !metadata;
}

elements.connect.onclick = async () => {
  try {
    if (!navigator.hid) throw new Error("Web HID requires Chrome or Edge on localhost or HTTPS.");
    if (device) {
      stopCapture("reconnect");
      device.removeEventListener("inputreport", receive);
      if (device.opened) await device.close();
    }
    [device] = await navigator.hid.requestDevice({ filters: [] });
    if (!device) return;
    await device.open();
    device.addEventListener("inputreport", receive);
    elements.start.disabled = false;
    elements.status.textContent = JSON.stringify({ product: device.productName, vendor_id: device.vendorId, product_id: device.productId, collections: device.collections }, null, 2);
  } catch (error) {
    elements.status.textContent = String(error);
  }
};

function receive(event) {
  if (!active) return;
  const received = performance.now();
  const bytes = new Uint8Array(event.data.buffer, event.data.byteOffset, event.data.byteLength);
  records.push({ type: "report", host_ms: received - started, report_id: event.reportId, hex: Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("") });
  if (records.length >= limit) stopCapture("report_limit");
}

elements.start.onclick = () => {
  if (records.length && !confirm("Replace the previous capture? Export it first if needed.")) return;
  limit = Number(elements.limit.value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 2000000) return;
  records = [];
  started = performance.now();
  metadata = { type: "metadata", schema_version: 1, created_utc: new Date().toISOString(), time_origin_ms: performance.timeOrigin, start_performance_ms: started, vendor_id: device.vendorId, product_id: device.productId, product_name: device.productName, collections: device.collections, timestamp_source: "browser_delivery", payload_excludes_report_id: true };
  active = true;
  elements.start.disabled = true;
  elements.stop.disabled = false;
  elements.export.disabled = true;
};
elements.stop.onclick = () => stopCapture();
elements.export.onclick = () => {
  const lines = [metadata, ...records, { type: "end", reports: records.length, reason }];
  const blob = new Blob(lines.map(record => JSON.stringify(record) + "\n"), { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `capture-${metadata.created_utc.replaceAll(":", "-")}.jsonl`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
navigator.hid?.addEventListener("disconnect", event => {
  if (event.device === device) stopCapture("disconnect");
});
setInterval(() => {
  if (metadata) elements.status.textContent = `${active ? "Recording" : "Stopped"}\nReports: ${records.length}\nElapsed: ${((performance.now() - started) / 1000).toFixed(1)} s\nStop reason: ${active ? "-" : reason}`;
}, 500);

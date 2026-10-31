// Read-only. Paste into the browser console on platform.ambitus.ai (signed in as super-admin).
// Downloads dsscan_names_<env>.json: {files: {file_id: relative path}, packages: {package_id: title}} for one
// Data Collection environment, from GET /api/data-collection/admin/files (the "All files" view). No writes.
(async () => {
  const ENV = "e392aa7b-0752-4697-8c4f-71ce1324fb7e";
  const out = { environment_id: ENV, files: {}, packages: {} };
  for (let page = 1; page <= 1000; page++) {
    const r = await fetch(`/api/data-collection/admin/files?environment_id=${ENV}&page=${page}&page_size=500`,
                          { credentials: "include" });
    if (!r.ok) { console.log("stopped:", r.status, await r.text()); break; }
    const j = await r.json();
    for (const f of j.files) { out.files[f.file_id] = f.path; out.packages[f.package_id] = f.title; }
    console.log(`page ${page}: ${Object.keys(out.files).length} of ${j.total}`);
    if (page * j.page_size >= j.total) break;
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out)], { type: "application/json" }));
  a.download = `dsscan_names_${ENV.slice(0, 8)}.json`;
  a.click();
  console.log("downloaded", a.download, Object.keys(out.files).length, "files,", Object.keys(out.packages).length, "packages");
})();

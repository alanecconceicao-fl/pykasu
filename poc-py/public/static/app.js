(() => {
  const basePath = document.body.dataset.basePath || "";
  const statusUrl = document.body.dataset.refreshStatusUrl;
  const status = document.getElementById("refresh-status");
  const availableButton = document.getElementById("updates-available");
  let renderedSequence = Number(document.body.dataset.renderedSequence || "0");
  let timer = null;

  async function checkRefresh() {
    try {
      const response = await fetch(statusUrl, { headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("status unavailable");
      const data = await response.json();
      const run = data.run || {};
      if (status) {
        if (data.active) {
          const completed = Number(run.source_completed || 0);
          const total = Number(run.source_total || 0);
          status.textContent = total ? `Refreshing sources (${completed}/${total})…` : "Refreshing saved sources…";
        } else if (run.status === "failed") {
          status.textContent = "Refresh stopped; saved updates remain available";
        } else {
          status.textContent = "Saved updates are ready";
        }
      }
      if (availableButton && Number(data.max_discovery_sequence || 0) > renderedSequence) {
        availableButton.hidden = false;
      }
      if (!data.active && timer) {
        window.clearInterval(timer);
        timer = null;
      }
    } catch (_) {
      if (status) status.textContent = "Saved updates are ready";
    }
  }

  async function loadAvailableUpdates() {
    const list = document.getElementById("updates-list");
    if (!list) return;
    const query = window.location.search || "";
    const response = await fetch(`${basePath}/updates/fragment${query}`, { headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    list.innerHTML = data.html;
    renderedSequence = Number(data.max_sequence || renderedSequence);
    document.body.dataset.renderedSequence = String(renderedSequence);
    availableButton.hidden = true;
  }

  if (availableButton) availableButton.addEventListener("click", loadAvailableUpdates);
  checkRefresh();
  timer = window.setInterval(checkRefresh, 2000);
})();

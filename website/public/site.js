"use strict";

const screenshotChoices = document.querySelectorAll(".screenshot-choices a");
const screenshot = document.getElementById("client-screenshot");
const fullSizeScreenshot = document.getElementById("screenshot-full-size");
screenshotChoices[0].setAttribute("aria-current", "true");
for (const choice of screenshotChoices) {
  choice.addEventListener("click", event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    screenshot.src = choice.href;
    screenshot.width = Number(choice.dataset.width);
    screenshot.height = Number(choice.dataset.height);
    screenshot.alt = choice.dataset.description;
    fullSizeScreenshot.href = choice.href;
    fullSizeScreenshot.setAttribute("aria-label", `View the ${choice.textContent} screenshot at full size`);
    for (const link of screenshotChoices) link.removeAttribute("aria-current");
    choice.setAttribute("aria-current", "true");
  });
}

// These packages belong to the release advertised on this page. Updating the
// release means updating the filenames and visible version together.
const release = "https://github.com/erikwb/blueferry/releases/download/v0.8.0/";
const distributions = {
  arch: { clients: ["gtk", "qt", "quickshell", "tui"], command: "sudo pacman -U", file: name => `${name}-0.8.0-1-any.pkg.tar.zst` },
  debian: { clients: ["gtk", "qt", "tui"], command: "sudo apt install", file: name => `${name}_0.8.0-1_all.deb` },
  ubuntu: { clients: ["gtk", "tui"], command: "sudo apt install", file: name => `${name}_0.8.0-1_all.deb` },
  fedora43: { clients: ["gtk", "qt", "tui"], command: "sudo dnf install", file: name => `${name}-0.8.0-1.fc43.noarch.rpm` },
  fedora45: { clients: ["gtk", "qt", "tui"], command: "sudo dnf install", file: name => `${name}-0.8.0-1.fc45.noarch.rpm` },
};
const clientLabels = { gtk: "GTK", qt: "KDE Plasma", quickshell: "Quickshell" };
const distributionSelect = document.getElementById("distribution");

function updateInstall() {
  const distribution = distributions[distributionSelect.value];
  const packages = [
    { name: "blueferry-backend", label: "Backend + Terminal" },
    ...distribution.clients.filter(client => client !== "tui").map(client => ({
      name: `blueferry-${client}`, label: clientLabels[client],
    })),
  ];
  const links = packages.map(({ name, label }) => {
    const link = document.createElement("a");
    link.href = release + distribution.file(name);
    link.textContent = label;
    return link;
  });
  document.getElementById("package-downloads").replaceChildren(...links);
  const distributionLabel = distributionSelect.selectedOptions[0].textContent;
  document.getElementById("package-summary").textContent = `BlueFerry 0.8.0 packages for ${distributionLabel}:`;
  document.getElementById("package-command").textContent = `${distribution.command} ./${distribution.file("blueferry-*")}`;
}

updateInstall();
distributionSelect.addEventListener("change", updateInstall);
document.getElementById("install-controls").hidden = false;
document.getElementById("install-fallback").hidden = true;

const copyStatus = document.getElementById("copy-status");
let copyTimeout;
for (const button of document.querySelectorAll("[data-copy]")) {
  button.hidden = false;
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    clearTimeout(copyTimeout);
    try {
      await navigator.clipboard.writeText(source.textContent);
      copyStatus.textContent = "Command copied.";
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(source);
      selection.removeAllRanges();
      selection.addRange(range);
      copyStatus.textContent = "Command selected. Press Ctrl+C to copy.";
    }
    copyTimeout = setTimeout(() => { copyStatus.textContent = ""; }, 3500);
  });
}

const status = document.getElementById("status");
const startButton = document.getElementById("start");
const sendButton = document.getElementById("send");
const comicButton = document.getElementById("comic");

function render(result) {
  if (result?.ok) {
    const chapters = Number(result.chapters || 0);
    status.textContent = chapters > 1
      ? `已读取 ${chapters} 章，共 ${result.chars} 个字符`
      : `已读取当前章节，共 ${result.chars} 个字符`;
    status.className = "ok";
  } else {
    status.textContent = result?.error || "操作失败";
    status.className = "error";
  }
}

async function run(message, pendingText) {
  status.textContent = pendingText;
  status.className = "";
  const result = await chrome.runtime.sendMessage({ type: message });
  render(result);
}

startButton.addEventListener("click", () => run("AMADEUS_START_READING", "正在读取当前章节…"));
comicButton.addEventListener("click", async () => {
  status.textContent = "正在抓取当前漫画章节…";
  status.className = "";
  const result = await chrome.runtime.sendMessage({ type: "AMADEUS_START_COMIC" });
  if (result?.ok) {
    status.textContent = `已抓取漫画章节，共 ${Number(result.pages || 0)} 页`;
    status.className = "ok";
  } else {
    status.textContent = result?.error || "漫画章节抓取失败";
    status.className = "error";
  }
});
sendButton.addEventListener("click", () => run("AMADEUS_SEND_ACTIVE_SELECTION", "正在发送选中文字…"));

chrome.storage.local.get("lastReadingStatus").then(({ lastReadingStatus }) => {
  if (lastReadingStatus) render(lastReadingStatus);
});

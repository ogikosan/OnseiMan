document.querySelectorAll(".copy").forEach((button) => {
  button.addEventListener("click", async () => {
    const code = button.parentElement.querySelector("code");
    const text = code.textContent.trim();

    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand("copy");
      selection.removeAllRanges();
    }

    const previous = button.textContent;
    button.textContent = "コピー済み";
    setTimeout(() => {
      button.textContent = previous;
    }, 1200);
  });
});

function refreshCounter(textarea) {
    const target = document.getElementById(textarea.dataset.countTarget);
    if (!target) return;
    const count = textarea.value.trim().length;
    target.textContent = `${count}/320`;
    target.classList.toggle("over", count > 320);
    const form = textarea.closest("form");
    const approve = form ? form.querySelector('button[value="approve"]') : null;
    if (approve) {
        approve.disabled = count === 0 || count > 320;
        approve.title = count > 320 ? "Shorten to 320 characters or fewer." : "";
    }
}

document.querySelectorAll("textarea[data-count-target]").forEach((textarea) => {
    refreshCounter(textarea);
    textarea.addEventListener("input", () => refreshCounter(textarea));
});


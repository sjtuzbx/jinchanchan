function downloadCsv() {
    const form = document.getElementById('screener-form');
    if (!form) {
        console.warn('screener form not found');
        return;
    }
    const params = new URLSearchParams(new FormData(form)).toString();
    window.location.href = `/screener/export?${params}`;
}

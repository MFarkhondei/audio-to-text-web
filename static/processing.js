(() => {
    const form = document.getElementById('conversionForm');
    const button = document.getElementById('submitBtn');
    const progressContainer = document.getElementById('progressContainer');
    const progressBar = document.getElementById('progressBar');
    const progressStatus = document.getElementById('progressStatus');
    const message = document.getElementById('processingMessage');
    let activeJob = null;
    let lastJob = null;

    function newId() {
        if (crypto.randomUUID) return crypto.randomUUID();
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }

    function setBusy(busy) {
        form.querySelectorAll('input, select').forEach(field => { field.disabled = busy; });
        button.type = busy ? 'button' : 'submit';
        button.classList.toggle('is-processing', busy);
        button.textContent = busy ? 'توقف پردازش' : 'تبدیل به متن';
        button.disabled = false;
    }

    function showMessage(text = '') {
        message.textContent = text;
        message.hidden = !text;
    }

    function showProgress(data) {
        const percent = Math.min(100, Math.max(0, Number(data.percent) || 0));
        progressContainer.style.display = 'block';
        progressBar.style.width = `${percent}%`;
        progressStatus.textContent = `${percent}% - ${data.status}`;
    }

    async function downloadLinks(links, job = null) {
        for (const link of links) {
            if (job && (job.stopRequested || job.superseded)) return;
            link.click();
            await new Promise(resolve => setTimeout(resolve, 500));
        }
    }

    function renderResults(job, results, autoDownload) {
        const fresh = results.filter(result => !job.seen.has(result.url));
        if (!fresh.length) return;
        if (!job.resultsList) {
            const section = document.createElement('section');
            section.className = 'results';
            section.setAttribute('aria-labelledby', 'resultsTitle');
            const title = document.createElement('h3');
            title.id = 'resultsTitle';
            title.textContent = 'خروجی فایل‌ها آماده است';
            const note = document.createElement('p');
            note.textContent = 'خروجی هر فایل پس از پایان پردازش آن آماده می‌شود. برای دانلود دوباره یا در صورت محدودیت دانلود خودکار مرورگر، از لینک‌های زیر استفاده کنید:';
            const list = document.createElement('ul');
            section.append(title, note, list);
            form.parentElement.insertBefore(section, form.parentElement.querySelector('.note'));
            job.resultsList = list;
        }
        const links = fresh.map(result => {
            job.seen.add(result.url);
            const item = document.createElement('li');
            const link = document.createElement('a');
            link.className = 'transcript-download';
            link.href = result.url;
            link.download = result.filename;
            link.append(document.createTextNode('دانلود '));
            const filename = document.createElement('bdi');
            filename.textContent = result.filename;
            link.append(filename);
            item.append(link);
            job.resultsList.append(item);
            return link;
        });
        if (autoDownload) job.downloadQueue = job.downloadQueue.then(() => downloadLinks(links, job));
    }

    function finish(job, data) {
        if (activeJob !== job) return;
        clearTimeout(job.timer);
        activeJob = null;
        setBusy(false);
        showProgress(data);
        showMessage(data.state === 'error' ? data.status : '');
        renderResults(job, data.results || [], !job.stopRequested);
    }

    async function poll(job) {
        if (activeJob !== job || job.stopping) return;
        try {
            const response = await fetch(`/jobs/${job.id}`, { cache: 'no-store' });
            const data = await response.json();
            if (activeJob !== job || job.stopping) return;
            if (response.ok) {
                if (!job.stopRequested) showMessage();
                showProgress(data);
                renderResults(job, data.results || [], !job.stopRequested);
                if (['completed', 'cancelled', 'error'].includes(data.state)) {
                    finish(job, data);
                    return;
                }
            } else {
                showMessage('وضعیت پردازش در دسترس نیست. می‌توانید دوباره برای توقف تلاش کنید.');
            }
        } catch (error) {
            if (activeJob !== job || job.stopping) return;
            showMessage('ارتباط با برنامه قطع شد. وضعیت دوباره بررسی می‌شود؛ دکمهٔ توقف همچنان در دسترس است.');
        }
        if (activeJob === job && !job.stopping) job.timer = setTimeout(() => poll(job), 800);
    }

    async function stop(job) {
        if (activeJob !== job || job.stopping) return;
        job.stopping = true;
        job.stopRequested = true;
        clearTimeout(job.timer);
        button.disabled = true;
        button.textContent = 'در حال توقف...';
        job.upload.abort();
        try {
            const response = await fetch(`/jobs/${job.id}/cancel`, { method: 'POST' });
            const data = await response.json();
            if (activeJob !== job) return;
            if (!response.ok) throw new Error(data.status);
            finish(job, data);
        } catch (error) {
            if (activeJob !== job) return;
            job.stopping = false;
            button.disabled = false;
            button.textContent = 'تلاش دوباره برای توقف';
            showMessage(error.message || 'توقف پردازش تأیید نشد؛ دوباره تلاش کنید.');
            job.timer = setTimeout(() => poll(job), 800);
        }
    }

    button.addEventListener('click', event => {
        if (activeJob) {
            event.preventDefault();
            stop(activeJob);
        }
    });

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (activeJob) {
            stop(activeJob);
            return;
        }
        const data = new FormData(form);
        if (lastJob) lastJob.superseded = true;
        const job = { id: newId(), upload: new AbortController(), stopping: false, stopRequested: false, timer: null,
            seen: new Set(), downloadQueue: Promise.resolve(), superseded: false };
        activeJob = job;
        lastJob = job;
        document.querySelectorAll('.results').forEach(section => section.remove());
        showMessage();
        setBusy(true);
        showProgress({ percent: 0, status: 'در حال دریافت فایل‌ها...' });
        try {
            const response = await fetch(`/jobs/${job.id}`, { method: 'POST', body: data, signal: job.upload.signal });
            const result = await response.json();
            if (activeJob !== job || job.stopping) return;
            renderResults(job, result.results || [], !job.stopRequested);
            if (!response.ok || ['completed', 'cancelled', 'error'].includes(result.state)) {
                finish(job, result);
                return;
            }
            poll(job);
        } catch (error) {
            if (activeJob !== job || job.stopping) return;
            showMessage('ارتباط هنگام ارسال فایل قطع شد. برای توقف درخواست، دکمهٔ توقف را بزنید.');
            poll(job);
        }
    });

    downloadLinks(document.querySelectorAll('.transcript-download'));
})();

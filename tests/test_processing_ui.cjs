const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/processing.js'), 'utf8');

function harness() {
    const h = { calls: [], downloads: [], sections: [], timers: new Map(), fields: [{ disabled: false }], nextId: 0 };
    class Element {
        constructor(tag) {
            this.tag = tag;
            this.style = {};
            this.handlers = {};
            this.children = [];
            this.disabled = false;
            this.textContent = '';
            this.classList = { toggle() {} };
        }
        addEventListener(event, callback) { this.handlers[event] = callback; }
        querySelectorAll() { return h.fields; }
        append(...children) { this.children.push(...children); }
        setAttribute() {}
        click() { if (this.tag === 'a') h.downloads.push(this.download); }
        remove() { h.sections = h.sections.filter(section => section !== this); }
    }
    const ids = Object.fromEntries(['conversionForm', 'submitBtn', 'progressContainer', 'progressBar', 'progressStatus', 'processingMessage']
        .map(id => [id, new Element(id)]));
    h.button = ids.submitBtn;
    h.message = ids.processingMessage;
    h.progress = ids.progressStatus;
    const parent = { insertBefore(section) { h.sections.push(section); }, querySelector() { return null; } };
    ids.conversionForm.parentElement = parent;
    const response = (data, ok = true, status = 200) => ({ ok, status, json: async () => data });
    h.post = { state: 'processing', percent: 0, status: 'processing', results: [] };
    h.get = { state: 'processing', percent: 20, status: 'processing', results: [] };
    h.cancel = { state: 'cancelled', percent: 20, status: 'پردازش متوقف شد.', results: [] };
    let timerId = 0;
    const context = {
        document: {
            getElementById: id => ids[id],
            createElement: tag => new Element(tag),
            createTextNode: text => ({ textContent: text }),
            querySelectorAll: selector => selector === '.results' ? [...h.sections] : [],
        },
        crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(++h.nextId).padStart(12, '0')}` },
        AbortController,
        FormData: class { constructor() { assert.equal(h.fields[0].disabled, false, 'capture form before disabling fields'); } },
        setTimeout(callback) { h.timers.set(++timerId, callback); return timerId; },
        clearTimeout(id) { h.timers.delete(id); },
        async fetch(url, options = {}) {
            h.calls.push({ url, options });
            if (url.endsWith('/cancel')) return response(h.cancel, !h.cancelFailed, h.cancelFailed ? 500 : 200);
            if (options.method === 'POST') {
                if (h.pendingUpload) return new Promise(resolve => { h.resolveUpload = data => resolve(response(data)); });
                return response(h.post, h.post.state !== 'error', h.post.state === 'error' ? 400 : 202);
            }
            if (h.pendingPoll) return new Promise(resolve => { h.resolvePoll = data => resolve(response(data)); });
            return response(h.get);
        },
    };
    vm.runInNewContext(source, context);
    h.submit = () => ids.conversionForm.handlers.submit({ preventDefault() {} });
    h.stop = () => ids.submitBtn.handlers.click({ preventDefault() {} });
    h.flush = async () => { for (let i = 0; i < 3; i++) await new Promise(setImmediate); };
    h.advance = async () => {
        const callbacks = [...h.timers.values()];
        h.timers.clear();
        callbacks.forEach(callback => callback());
        await h.flush();
    };
    return h;
}

module.exports = async function runTests() {
    const uploading = harness();
    uploading.pendingUpload = true;
    const uploadRequest = uploading.submit();
    assert.equal(uploading.button.textContent, 'توقف پردازش');
    assert.equal(uploading.button.disabled, false);
    assert.equal(uploading.fields[0].disabled, true);
    uploading.stop();
    assert.equal(uploading.button.textContent, 'در حال توقف...');
    await uploading.flush();
    assert.equal(uploading.calls[0].options.signal.aborted, true);
    assert.equal(uploading.button.textContent, 'تبدیل به متن');
    assert.equal(uploading.fields[0].disabled, false);
    uploading.resolveUpload({ state: 'processing', percent: 40, status: 'late response' });
    await uploadRequest;
    assert.equal(uploading.button.textContent, 'تبدیل به متن');
    assert.equal(uploading.downloads.length, 0);

    const stale = harness();
    stale.pendingPoll = true;
    await stale.submit();
    const resolveOldPoll = stale.resolvePoll;
    const firstId = stale.calls[0].url;
    stale.stop();
    await stale.flush();
    assert.equal(stale.calls.find(call => call.url.endsWith('/cancel')).url, `${firstId}/cancel`);
    stale.pendingPoll = false;
    await stale.submit();
    resolveOldPoll({ state: 'completed', percent: 100, status: 'old result', results: [{ filename: 'old.txt', url: '/old' }] });
    await stale.flush();
    assert.equal(stale.button.textContent, 'توقف پردازش');
    assert.equal(stale.downloads.length, 0);
    assert.equal(stale.nextId, 2);

    const completed = harness();
    completed.get = { state: 'completed', percent: 100, status: 'done', results: [
        { filename: 'one.txt', url: '/one' }, { filename: 'two.txt', url: '/two' },
    ] };
    await completed.submit();
    await completed.flush();
    await completed.advance();
    assert.deepEqual(completed.downloads, ['one.txt', 'two.txt']);
    assert.equal(completed.button.textContent, 'تبدیل به متن');
    assert.equal(completed.button.disabled, false);
    assert.equal(completed.sections.length, 1);

    const incremental = harness();
    const one = { filename: 'same.txt', url: '/first' };
    const two = { filename: 'same.txt', url: '/second' };
    incremental.get.results = [one];
    await incremental.submit();
    await incremental.flush();
    assert.deepEqual(incremental.downloads, ['same.txt'], 'first file downloads before the batch finishes');
    assert.equal(incremental.button.textContent, 'توقف پردازش');
    await incremental.advance();
    assert.equal(incremental.downloads.length, 1, 'repeated progress must not redownload a file');
    incremental.get.results = [one, two];
    await incremental.advance();
    assert.equal(incremental.downloads.length, 2, 'same filenames with distinct URLs download independently');
    incremental.get.state = 'completed';
    await incremental.advance();
    assert.equal(incremental.downloads.length, 2, 'completion must not repeat prior downloads');
    assert.equal(incremental.sections.length, 1);
    assert.equal(incremental.sections[0].children[2].children.length, 2);

    for (const state of ['cancelled', 'error']) {
        const partial = harness();
        partial.get.results = [one];
        await partial.submit();
        await partial.flush();
        if (state === 'cancelled') {
            partial.cancel.results = [one, two];
            partial.stop();
            await partial.flush();
        } else {
            partial.get = { state, percent: 50, status: 'second failed', results: [one] };
            await partial.advance();
        }
        assert.equal(partial.sections.length, 1, 'completed links remain on stop/error');
        assert.equal(partial.downloads.length, 1);
        assert.equal(partial.button.textContent, 'تبدیل به متن');
    }

    const failed = harness();
    failed.post = { state: 'error', percent: 0, status: 'ffmpeg is missing', results: [] };
    await failed.submit();
    assert.equal(failed.button.textContent, 'تبدیل به متن');
    assert.equal(failed.message.textContent, 'ffmpeg is missing');
    assert.equal(failed.downloads.length, 0);

    const retry = harness();
    await retry.submit();
    await retry.flush();
    retry.cancelFailed = true;
    retry.cancel = { state: 'processing', status: 'cannot stop' };
    retry.stop();
    await retry.flush();
    assert.equal(retry.button.textContent, 'تلاش دوباره برای توقف');
    assert.equal(retry.button.disabled, false);
    assert.equal(retry.fields[0].disabled, true);
    retry.cancelFailed = false;
    retry.cancel = { state: 'cancelled', percent: 20, status: 'stopped', results: [] };
    retry.stop();
    await retry.flush();
    assert.equal(retry.button.textContent, 'تبدیل به متن');

    const alreadyDone = harness();
    await alreadyDone.submit();
    await alreadyDone.flush();
    alreadyDone.cancel = completed.get;
    alreadyDone.stop();
    await alreadyDone.flush();
    assert.equal(alreadyDone.downloads.length, 0, 'a stop request must suppress automatic downloads');
    assert.equal(alreadyDone.sections.length, 1, 'finished outputs remain available as manual links');

    return '9 UI scenarios passed, including incremental downloads, duplicate filenames, deduplication, and preserving completed links on stop/error.';
};

if (require.main === module) {
    module.exports().then(result => console.log(result)).catch(error => {
        console.error(error);
        process.exitCode = 1;
    });
}

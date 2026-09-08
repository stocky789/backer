// Run with: node tests/test_s3_files_web.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../src/backer/server/web/templates/repositories.html'), 'utf8');
const elements = new Map();
const get = id => {
    if (!elements.has(id)) elements.set(id, { value: 'test', style: {}, closest() { return this; } });
    return elements.get(id);
};
const payloads = [];
const errors = [];
const context = vm.createContext({
    document: { getElementById: get },
    fetch: async (_url, options) => {
        payloads.push(JSON.parse(options.body));
        return { ok: true, json: async () => ({}) };
    },
    showError: (_id, text) => errors.push(text),
    hideAddModal() {}, Toast: { success() {} }, setTimeout() {},
});
vm.runInContext(html.slice(html.indexOf('function onTypeChange()'), html.indexOf('function backToStep1()')), context);
(async () => {
    get('repoType').value = 's3';
    get('repositoryFormat').value = 'files';
    get('s3RepositoryPassword').value = '';
    context.onTypeChange();
    assert.equal(get('repoType').value, 's3');
    assert.equal(get('s3Config').style.display, 'block');
    assert.equal(get('s3RepositoryPassword').required, false);
    await context.saveS3Repository();
    assert.equal(payloads[0].format, 'files');
    assert.equal('repository_password' in payloads[0], false);
    assert.equal(errors.length, 0);
    get('repositoryFormat').value = 'kopia';
    await context.saveS3Repository();
    assert.equal(payloads.length, 1);
    assert.equal(errors.length, 1);
    get('s3RepositoryPassword').value = 'password';
    await context.saveS3Repository();
    assert.equal(payloads[1].repository_password, 'password');
    console.log('1 passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

import { writeFileSync } from 'node:fs';
const commit = process.env.APP_COMMIT_SHA ?? '';
if (commit && !/^[a-f0-9]{40}$/.test(commit)) throw new Error('INVALID_BUILD_COMMIT');
writeFileSync('dist/server/server/build-metadata.json', JSON.stringify({ commit }));

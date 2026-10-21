// Runs one Action read as JSON from stdin, for the editor's Test button.
// Exits 0 on success; on failure prints the error to stderr and exits 1.
import { runAction } from './actions.js'

console.log = console.error

try {
	const chunks = []
	for await (const c of process.stdin) chunks.push(c)
	const action = JSON.parse(Buffer.concat(chunks).toString('utf8'))
	await runAction(action, { onBuiltin: (name) => console.error(`builtin ${name} only runs in the daemon, skipped`) })
	process.exit(0)
} catch (e) {
	console.error(e?.message ?? e)
	process.exit(1)
}

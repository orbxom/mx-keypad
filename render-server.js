// Renders key previews for the editor. Usage: node render-server.js <configDir>
// stdin:  {"id":1,"key":Key,"pressed":false} per line
// stdout: {"id":1,"png":"<base64>"} or {"id":1,"error":"..."} per line. Logs go to stderr.
import { createInterface } from 'node:readline'
import { resolve } from 'node:path'
import { renderKey } from './render.js'

console.log = console.error // keep stdout for replies only
const baseDir = resolve(process.argv[2] ?? '.')
let chain = Promise.resolve()

async function handle(line) {
	let id = null
	try {
		const req = JSON.parse(line)
		id = req.id ?? null
		const png = await renderKey(req.key ?? {}, { pressed: !!req.pressed, format: 'png', baseDir })
		return { id, png: png.toString('base64') }
	} catch (e) {
		return { id, error: e?.message ?? String(e) }
	}
}

createInterface({ input: process.stdin }).on('line', (line) => {
	if (!line.trim()) return
	chain = chain.then(async () => process.stdout.write(JSON.stringify(await handle(line)) + '\n'))
})

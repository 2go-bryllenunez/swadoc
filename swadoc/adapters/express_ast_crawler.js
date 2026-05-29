#!/usr/bin/env node
/**
 * express_ast_crawler.js
 *
 * Static AST crawl of an Express project to discover route registrations.
 * Does NOT execute any application code — pure file-system + regex analysis.
 *
 * Usage:
 *   node express_ast_crawler.js <project_path>
 *
 * Output:
 *   JSON array written to stdout, each element:
 *   {
 *     "method":            "GET" | "POST" | ...,
 *     "uri":               "/path/:param",
 *     "handler":           "Controller.method" | "<anonymous>",
 *     "handler_file":      "/abs/path/to/file.js" | null,
 *     "handler_function":  "functionName" | null
 *   }
 *
 * No npm dependencies — only Node.js built-ins (fs, path).
 */

'use strict';

const fs   = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** HTTP methods recognised as Express route registrations. */
const HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'all'];

/** File extensions to crawl. */
const JS_EXTENSIONS = new Set(['.js', '.mjs', '.cjs', '.ts', '.mts', '.cts']);

/** Directories to skip entirely. */
const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'build', 'coverage', '.nyc_output', 'test', 'tests', '__tests__', 'spec', 'specs']);

// ---------------------------------------------------------------------------
// Regex patterns
// ---------------------------------------------------------------------------

/**
 * Matches route registrations of the form:
 *   app.get('/path', handler)
 *   router.post('/path', mw1, mw2, handler)
 *   app.use('/path', handler)
 *   Router.get('/path', handler)
 *
 * Capture groups:
 *   1 – HTTP method (or "use")
 *   2 – route path (single or double quoted, or template literal)
 *   3 – remainder of the argument list (handlers)
 */
const ROUTE_PATTERN = new RegExp(
  // object name (app / router / Router / this.router / module.exports.router …)
  '(?:^|[^\\w])' +
  '(?:[a-zA-Z_$][\\w$]*\\.)*' +   // optional chained prefix
  '(?:router|app|Router)' +
  '\\.(' + HTTP_METHODS.join('|') + '|use)' +
  '\\s*\\(' +
  '\\s*' +
  // route path: single-quoted, double-quoted, or template literal
  '([\'"`])([^\'"` ][^\'"`]*?)\\2' +
  '\\s*,' +
  // capture the rest of the argument list up to the closing paren
  '([^;{]*)',
  'gm'
);

/**
 * Matches a named function reference used as a handler:
 *   someFunction
 *   SomeController.someMethod
 *   module.exports.handler
 */
const NAMED_HANDLER_PATTERN = /\b([a-zA-Z_$][\w$]*)(?:\.([a-zA-Z_$][\w$]*))*\s*(?=[,)]|$)/;

/**
 * Matches an inline arrow function or function expression:
 *   (req, res) => { … }
 *   function(req, res) { … }
 *   async (req, res) => { … }
 */
const INLINE_HANDLER_PATTERN = /(?:async\s+)?(?:function\s*\(|(?:\([^)]*\)|\w+)\s*=>)/;

/**
 * Matches a require() call:
 *   require('./controllers/user')
 *   require('../handlers')
 */
const REQUIRE_PATTERN = /(?:const|let|var)\s+([\w$]+)\s*=\s*require\s*\(\s*['"`]([^'"`]+)['"`]\s*\)/g;

/**
 * Matches a destructured require:
 *   const { getUser, createUser } = require('./controllers/user')
 */
const DESTRUCTURE_REQUIRE_PATTERN = /(?:const|let|var)\s+\{([^}]+)\}\s*=\s*require\s*\(\s*['"`]([^'"`]+)['"`]\s*\)/g;

/**
 * Matches a named function declaration:
 *   function myHandler(req, res) {
 *   async function myHandler(req, res) {
 *   const myHandler = (req, res) => {
 *   const myHandler = async (req, res) => {
 *   const myHandler = function(req, res) {
 */
const FUNCTION_DECL_PATTERN = /(?:^|\n)\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(/;
const ARROW_DECL_PATTERN     = /(?:^|\n)\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*=>/;
const METHOD_DECL_PATTERN    = /(?:^|\n)\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{/;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Recursively collect all JS/TS files under `dir`, skipping SKIP_DIRS.
 * @param {string} dir
 * @returns {string[]}
 */
function collectFiles(dir) {
  const results = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (_) {
    return results;
  }
  for (const entry of entries) {
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) {
        results.push(...collectFiles(path.join(dir, entry.name)));
      }
    } else if (entry.isFile() && JS_EXTENSIONS.has(path.extname(entry.name).toLowerCase())) {
      results.push(path.join(dir, entry.name));
    }
  }
  return results;
}

/**
 * Read a file safely; return empty string on error.
 * @param {string} filePath
 * @returns {string}
 */
function readFileSafe(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf8');
  } catch (_) {
    return '';
  }
}

/**
 * Resolve a require path relative to the current file, returning an absolute
 * path (without extension) or null when the path is not relative.
 * @param {string} requirePath
 * @param {string} fromFile
 * @param {string} projectRoot
 * @returns {string|null}
 */
function resolveRequire(requirePath, fromFile, projectRoot) {
  if (!requirePath.startsWith('.')) return null; // external module
  const base = path.resolve(path.dirname(fromFile), requirePath);
  // Try exact path first, then common extensions
  const candidates = [
    base,
    base + '.js',
    base + '.ts',
    base + '.mjs',
    base + '.cjs',
    path.join(base, 'index.js'),
    path.join(base, 'index.ts'),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c) && fs.statSync(c).isFile()) return c;
  }
  return null;
}

/**
 * Build a map of { localName → absoluteFilePath } from require() calls in
 * the given source text.
 * @param {string} source
 * @param {string} filePath
 * @param {string} projectRoot
 * @returns {Map<string, string>}
 */
function buildRequireMap(source, filePath, projectRoot) {
  const map = new Map();

  // Simple require: const foo = require('./foo')
  let m;
  REQUIRE_PATTERN.lastIndex = 0;
  while ((m = REQUIRE_PATTERN.exec(source)) !== null) {
    const localName = m[1];
    const resolved  = resolveRequire(m[2], filePath, projectRoot);
    if (resolved) map.set(localName, resolved);
  }

  // Destructured require: const { a, b } = require('./foo')
  DESTRUCTURE_REQUIRE_PATTERN.lastIndex = 0;
  while ((m = DESTRUCTURE_REQUIRE_PATTERN.exec(source)) !== null) {
    const names   = m[1].split(',').map(s => s.trim().split(/\s+as\s+/).pop().trim()).filter(Boolean);
    const resolved = resolveRequire(m[2], filePath, projectRoot);
    if (resolved) {
      for (const name of names) {
        map.set(name, resolved);
      }
    }
  }

  return map;
}

/**
 * Given a handler argument string (the last argument in a route registration),
 * attempt to resolve it to { handlerFile, handlerFunction }.
 *
 * @param {string} handlerArg   – raw text of the last argument
 * @param {Map<string,string>} requireMap – local-name → file path
 * @param {string} currentFile  – absolute path of the file being parsed
 * @returns {{ handlerFile: string|null, handlerFunction: string|null, handler: string }}
 */
function resolveHandler(handlerArg, requireMap, currentFile) {
  const trimmed = handlerArg.trim().replace(/[,)]+$/, '').trim();

  // Inline function — unresolvable
  if (INLINE_HANDLER_PATTERN.test(trimmed)) {
    return { handlerFile: null, handlerFunction: null, handler: '<anonymous>' };
  }

  // Named reference: could be "fn", "obj.method", "Obj.method.bind(this)"
  const parts = trimmed.split('.').map(p => p.replace(/\(.*$/, '').trim()).filter(Boolean);

  if (parts.length === 0) {
    return { handlerFile: null, handlerFunction: null, handler: trimmed || '<anonymous>' };
  }

  const rootName = parts[0];
  const methodName = parts.length > 1 ? parts[parts.length - 1] : null;

  // Check if rootName is a known require
  const resolvedFile = requireMap.get(rootName) || null;

  const handlerLabel = parts.join('.');

  return {
    handlerFile:     resolvedFile,
    handlerFunction: methodName || rootName,
    handler:         handlerLabel,
  };
}

/**
 * Normalise a route path:
 *   - Express :param → keep as-is (already OpenAPI-compatible with colon notation)
 *   - Ensure leading slash
 * @param {string} rawPath
 * @returns {string}
 */
function normalisePath(rawPath) {
  if (!rawPath.startsWith('/')) return '/' + rawPath;
  return rawPath;
}

// ---------------------------------------------------------------------------
// Core crawl logic
// ---------------------------------------------------------------------------

/**
 * Parse a single file and return all route registrations found.
 * @param {string} filePath
 * @param {string} projectRoot
 * @returns {Array<{method,uri,handler,handler_file,handler_function}>}
 */
function parseFile(filePath, projectRoot) {
  const source     = readFileSafe(filePath);
  if (!source) return [];

  const requireMap = buildRequireMap(source, filePath, projectRoot);
  const routes     = [];

  // Reset regex state
  ROUTE_PATTERN.lastIndex = 0;

  let match;
  while ((match = ROUTE_PATTERN.exec(source)) !== null) {
    const method    = match[1].toUpperCase();
    const rawPath   = match[3]; // group 3 is the path content (inside quotes)
    const argsRest  = match[4] || ''; // group 4 is the rest of the args

    // Skip app.use() without a path that looks like a sub-router mount
    // (we still include app.use('/prefix', router) style mounts as they
    //  represent route prefixes — but we can't expand them statically)
    const uri = normalisePath(rawPath);

    // The last non-empty token in argsRest is the handler
    // Split on commas but be careful of nested parens
    const handlerArg = extractLastArg(argsRest);

    const { handlerFile, handlerFunction, handler } = resolveHandler(
      handlerArg,
      requireMap,
      filePath
    );

    routes.push({
      method:           method === 'ALL' ? 'GET' : method, // normalise "all"
      uri,
      handler,
      handler_file:     handlerFile,
      handler_function: handlerFunction,
    });
  }

  return routes;
}

/**
 * Extract the last argument from a comma-separated argument list string,
 * respecting nested parentheses.
 * @param {string} argsStr
 * @returns {string}
 */
function extractLastArg(argsStr) {
  // Walk backwards through the string collecting the last argument
  let depth = 0;
  let end   = argsStr.length;

  // Trim trailing whitespace / closing paren
  let i = argsStr.length - 1;
  while (i >= 0 && /[\s),]/.test(argsStr[i])) i--;
  end = i + 1;

  // Now find the start of this last argument
  let start = 0;
  for (let j = end - 1; j >= 0; j--) {
    const ch = argsStr[j];
    if (ch === ')' || ch === ']') { depth++; continue; }
    if (ch === '(' || ch === '[') { depth--; continue; }
    if (depth === 0 && ch === ',') { start = j + 1; break; }
  }

  return argsStr.slice(start, end).trim();
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

function main() {
  const projectPath = process.argv[2];
  if (!projectPath) {
    process.stderr.write('Usage: node express_ast_crawler.js <project_path>\n');
    process.exit(1);
  }

  const absProjectPath = path.resolve(projectPath);
  if (!fs.existsSync(absProjectPath)) {
    process.stderr.write(`Project path does not exist: ${absProjectPath}\n`);
    process.exit(1);
  }

  const files  = collectFiles(absProjectPath);
  const routes = [];

  for (const file of files) {
    const found = parseFile(file, absProjectPath);
    routes.push(...found);
  }

  // Deduplicate by method+uri+handler_file+handler_function
  const seen = new Set();
  const unique = routes.filter(r => {
    const key = `${r.method}|${r.uri}|${r.handler_file}|${r.handler_function}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  process.stdout.write(JSON.stringify(unique, null, 2) + '\n');
}

main();

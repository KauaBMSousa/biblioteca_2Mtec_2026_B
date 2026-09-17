// Extract resolved call edges from JS/TS files using the TypeScript
// compiler API, and print them as the JSON shape every semantic backend
// returns.
//
// The checker is what makes this exact: `handler.run()` resolves through
// the receiver's inferred type, so the edge points at the real declaration
// instead of at whatever else happens to be named `run`.
//
// Usage: node _tsc_extract.js <root> <file...>   (files relative to root)

const path = require("path");
const ts = require("typescript");

const [, , root, ...files] = process.argv;
if (!root || files.length === 0) {
    console.log(JSON.stringify({ edges: [], dependencies: {}, failures: {} }));
    process.exit(0);
}

const absolute = files.map((file) => path.resolve(root, file));

// A tsconfig gives the right module resolution and lib set; without one the
// defaults still type-check plain JS well enough to resolve calls.
const configPath = ts.findConfigFile(root, ts.sys.fileExists, "tsconfig.json");
let options = { allowJs: true, checkJs: true, noEmit: true };
if (configPath) {
    const parsed = ts.parseJsonConfigFileContent(
        ts.readConfigFile(configPath, ts.sys.readFile).config,
        ts.sys,
        path.dirname(configPath)
    );
    options = { ...parsed.options, allowJs: true, checkJs: true, noEmit: true };
}

const program = ts.createProgram(absolute, options);
const checker = program.getTypeChecker();

const relative = (file) => {
    const rel = path.relative(root, file);
    return rel.startsWith("..") ? null : rel;
};

const edges = [];
const dependencies = {};
const failures = {};

for (const file of absolute) {
    const source = program.getSourceFile(file);
    const rel = relative(file);
    if (!source) {
        if (rel) failures[rel] = "not part of the TypeScript program";
        continue;
    }

    // What this file imports, for staleness: editing an imported module can
    // change what a call in this one resolves to.
    const deps = [];
    ts.forEachChild(source, (node) => {
        if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
            const resolved = ts.resolveModuleName(
                node.moduleSpecifier.text, file, options, ts.sys
            ).resolvedModule;
            if (resolved) {
                const depRel = relative(resolved.resolvedFileName);
                if (depRel) deps.push(depRel);
            }
        }
    });
    dependencies[rel] = deps;

    const visit = (node) => {
        if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
            const target = ts.isPropertyAccessExpression(node.expression)
                ? node.expression.name
                : node.expression;
            const symbol = checker.getSymbolAtLocation(target);
            const resolved = symbol && symbol.flags & ts.SymbolFlags.Alias
                ? checker.getAliasedSymbol(symbol)
                : symbol;
            const declaration = resolved && resolved.declarations && resolved.declarations[0];
            if (resolved) {
                const position = source.getLineAndCharacterOfPosition(target.getStart(source));
                let toFile = null;
                let toLine = null;
                if (declaration) {
                    const declarationSource = declaration.getSourceFile();
                    toFile = relative(declarationSource.fileName);
                    toLine = declarationSource.getLineAndCharacterOfPosition(
                        declaration.getStart(declarationSource)
                    ).line + 1;
                }
                edges.push({
                    from_file: rel,
                    from_line: position.line + 1,
                    from_column: position.character + 1,
                    to_name: resolved.getName(),
                    to_file: toFile,
                    to_line: toLine,
                });
            }
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
}

console.log(JSON.stringify({ edges, dependencies, failures }));

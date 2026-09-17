<?php
/**
 * Extract name-resolved call edges from PHP files using nikic/php-parser,
 * printing the JSON shape every semantic backend returns.
 *
 * The NameResolver visitor is what makes this more than grep: it rewrites
 * every call to its fully-qualified name using the file's namespace and
 * `use` statements, so `$logger->write()` and `Log\write()` stop looking
 * alike.
 *
 * Usage: php _php_extract.php <vendor/autoload.php> <root> <file...>
 */

$autoload = $argv[1] ?? null;
$root = $argv[2] ?? null;
$files = array_slice($argv, 3);

if (!$autoload || !$root || !$files) {
    echo json_encode(['edges' => [], 'dependencies' => [], 'failures' => []]);
    exit(0);
}
require $autoload;

use PhpParser\Error;
use PhpParser\Node;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\ParserFactory;

final class CallCollector extends NodeVisitorAbstract
{
    public array $edges = [];
    public array $uses = [];

    public function __construct(private string $relpath) {}

    public function enterNode(Node $node)
    {
        if ($node instanceof Node\Stmt\Use_) {
            foreach ($node->uses as $use) {
                $this->uses[] = $use->name->toString();
            }
            return null;
        }

        $name = null;
        if ($node instanceof Node\Expr\FuncCall && $node->name instanceof Node\Name) {
            // NameResolver has already qualified this against `use`.
            $name = $node->name->toString();
        } elseif ($node instanceof Node\Expr\MethodCall && $node->name instanceof Node\Identifier) {
            $name = $node->name->toString();
        } elseif ($node instanceof Node\Expr\StaticCall && $node->name instanceof Node\Identifier) {
            $class = $node->class instanceof Node\Name ? $node->class->toString() : '?';
            $name = $class . '::' . $node->name->toString();
        } elseif ($node instanceof Node\Expr\New_ && $node->class instanceof Node\Name) {
            $name = $node->class->toString();
        }

        if ($name !== null) {
            $this->edges[] = [
                'from_file' => $this->relpath,
                'from_line' => $node->getStartLine(),
                'from_column' => 1,
                'to_name' => $name,
                'to_file' => null,
                'to_line' => null,
            ];
        }
        return null;
    }
}

$parser = (new ParserFactory())->createForNewestSupportedVersion();
$edges = [];
$dependencies = [];
$failures = [];

foreach ($files as $relpath) {
    $path = rtrim($root, '/') . '/' . $relpath;
    $code = @file_get_contents($path);
    if ($code === false) {
        $failures[$relpath] = 'unreadable';
        continue;
    }
    try {
        $ast = $parser->parse($code);
    } catch (Error $e) {
        // A file that does not parse has no resolvable calls, and saying
        // so is the point: "0 edges" must not look like "not analyzable".
        $failures[$relpath] = 'parse error: ' . $e->getMessage();
        continue;
    }

    $collector = new CallCollector($relpath);
    $traverser = new NodeTraverser();
    $traverser->addVisitor(new NameResolver());
    $traverser->addVisitor($collector);
    $traverser->traverse($ast);

    $edges = array_merge($edges, $collector->edges);
    $dependencies[$relpath] = array_values(array_unique($collector->uses));
}

echo json_encode([
    'edges' => $edges,
    'dependencies' => $dependencies,
    'failures' => $failures,
]);

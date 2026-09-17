// Extract type-resolved call edges from Java sources using javac's own
// compiler tree API, printing the JSON shape every semantic backend
// returns.
//
// `Trees.getElement` is what makes this exact: it hands back the element a
// call actually binds to, after overload resolution and imports -- the same
// answer OpenRewrite's typed LST carries, obtained straight from the
// compiler.
//
// Usage: java _JavaExtract.java <root> <file...>   (single-file source mode)

import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MemberSelectTree;
import com.sun.source.tree.NewClassTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;

import javax.lang.model.element.Element;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

import java.io.File;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

public class _JavaExtract {

    record Edge(String fromFile, long fromLine, long fromColumn, String toName, String toFile, Long toLine) {
        String toJson() {
            StringBuilder json = new StringBuilder();
            json.append("{\"from_file\":\"").append(escape(fromFile)).append("\"")
                .append(",\"from_line\":").append(fromLine)
                .append(",\"from_column\":").append(fromColumn)
                .append(",\"to_name\":\"").append(escape(toName)).append("\"");
            json.append(",\"to_file\":").append(toFile == null ? "null" : "\"" + escape(toFile) + "\"");
            json.append(",\"to_line\":").append(toLine == null ? "null" : toLine);
            return json.append("}").toString();
        }
    }

    static String escape(String text) {
        return text == null ? "" : text.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.out.println("{\"edges\":[],\"dependencies\":{},\"failures\":{}}");
            return;
        }
        Path root = Paths.get(args[0]).toAbsolutePath();

        List<File> sources = new ArrayList<>();
        for (int i = 1; i < args.length; i++) {
            sources.add(root.resolve(args[i]).toFile());
        }

        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager fileManager = compiler.getStandardFileManager(null, null, null);
        Iterable<? extends JavaFileObject> units = fileManager.getJavaFileObjectsFromFiles(sources);

        JavacTask task = (JavacTask) compiler.getTask(null, fileManager, diagnostic -> { }, null, null, units);
        Iterable<? extends CompilationUnitTree> parsed = task.parse();
        task.analyze();

        Trees trees = Trees.instance(task);
        SourcePositions positions = trees.getSourcePositions();
        List<Edge> edges = new ArrayList<>();

        for (CompilationUnitTree unit : parsed) {
            Path unitPath = Paths.get(unit.getSourceFile().toUri());
            String relative = root.relativize(unitPath).toString();

            new TreePathScanner<Void, Void>() {
                void record(com.sun.source.tree.Tree node, String fallbackName) {
                    Element element = trees.getElement(getCurrentPath());
                    String name = element != null ? element.getSimpleName().toString() : fallbackName;
                    long start = positions.getStartPosition(unit, node);
                    long line = unit.getLineMap().getLineNumber(start);
                    long column = unit.getLineMap().getColumnNumber(start);

                    String toFile = null;
                    Long toLine = null;
                    if (element != null) {
                        var declarationPath = trees.getPath(element);
                        if (declarationPath != null) {
                            Path declaringFile = Paths.get(declarationPath.getCompilationUnit().getSourceFile().toUri());
                            if (declaringFile.startsWith(root)) {
                                toFile = root.relativize(declaringFile).toString();
                                long declarationStart = positions.getStartPosition(
                                    declarationPath.getCompilationUnit(), declarationPath.getLeaf());
                                toLine = declarationPath.getCompilationUnit()
                                    .getLineMap().getLineNumber(declarationStart);
                            }
                        }
                    }
                    edges.add(new Edge(relative, line, column, name, toFile, toLine));
                }

                @Override
                public Void visitMethodInvocation(MethodInvocationTree node, Void unused) {
                    String fallback = node.getMethodSelect() instanceof MemberSelectTree select
                        ? select.getIdentifier().toString()
                        : node.getMethodSelect().toString();
                    record(node, fallback);
                    return super.visitMethodInvocation(node, unused);
                }

                @Override
                public Void visitNewClass(NewClassTree node, Void unused) {
                    record(node, node.getIdentifier().toString());
                    return super.visitNewClass(node, unused);
                }
            }.scan(unit, null);
        }

        StringBuilder json = new StringBuilder("{\"edges\":[");
        for (int i = 0; i < edges.size(); i++) {
            if (i > 0) json.append(",");
            json.append(edges.get(i).toJson());
        }
        json.append("],\"dependencies\":{},\"failures\":{}}");
        System.out.println(json);
    }
}

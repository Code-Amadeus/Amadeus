#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int is_project_root(const char *root) {
    char script[PATH_MAX];
    char electron[PATH_MAX];
    if (snprintf(script, sizeof(script), "%s/scripts/start_wallpaper.sh", root) >= (int)sizeof(script)) return 0;
    if (snprintf(electron, sizeof(electron), "%s/electron/package.json", root) >= (int)sizeof(electron)) return 0;
    return access(script, R_OK) == 0 && access(electron, R_OK) == 0;
}

static int copy_project_root(char *output, size_t output_size, const char *candidate) {
    char resolved[PATH_MAX];
    if (!candidate || !candidate[0] || !realpath(candidate, resolved) || !is_project_root(resolved)) return 0;
    return snprintf(output, output_size, "%s", resolved) < (int)output_size;
}

static int read_project_root_file(char *output, size_t output_size, const char *path) {
    FILE *file = fopen(path, "r");
    if (!file) return 0;
    char line[PATH_MAX];
    char *value = fgets(line, sizeof(line), file);
    fclose(file);
    if (!value) return 0;
    line[strcspn(line, "\r\n")] = '\0';
    return copy_project_root(output, output_size, line);
}

static int parent_directory(char *path) {
    char *slash = strrchr(path, '/');
    if (!slash || slash == path) return 0;
    *slash = '\0';
    return 1;
}

static int resolve_project_root(char *output, size_t output_size) {
    if (copy_project_root(output, output_size, getenv("AMADEUS_PROJECT_ROOT"))) return 1;

    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) return 0;
    char resolved[PATH_MAX];
    if (!realpath(executable, resolved)) return 0;

    char contents[PATH_MAX];
    if (snprintf(contents, sizeof(contents), "%s", resolved) >= (int)sizeof(contents)) return 0;
    if (!parent_directory(contents) || !parent_directory(contents)) return 0;

    char root_file[PATH_MAX];
    if (snprintf(root_file, sizeof(root_file), "%s/Resources/project-root", contents) >= (int)sizeof(root_file)) return 0;
    if (read_project_root_file(output, output_size, root_file)) return 1;

    /* A bundle kept directly in a checkout remains usable after a local build. */
    char bundle_parent[PATH_MAX];
    if (snprintf(bundle_parent, sizeof(bundle_parent), "%s", contents) >= (int)sizeof(bundle_parent)) return 0;
    if (!parent_directory(bundle_parent) || !parent_directory(bundle_parent)) return 0;
    return copy_project_root(output, output_size, bundle_parent);
}

int main(int argc, char **argv) {
    char project_root[PATH_MAX];
    if (!resolve_project_root(project_root, sizeof(project_root))) {
        fprintf(stderr,
                "Amadeus Wallpaper could not find its project checkout. "
                "Rebuild the app with scripts/build_macos_wallpaper_app.sh.\n");
        return 1;
    }

    char script[PATH_MAX];
    if (snprintf(script, sizeof(script), "%s/scripts/start_wallpaper.sh", project_root) >= (int)sizeof(script)) {
        fprintf(stderr, "Amadeus project path is too long.\n");
        return 1;
    }

    char **arguments = calloc((size_t)argc + 2, sizeof(char *));
    if (!arguments) return 1;
    arguments[0] = "/bin/bash";
    arguments[1] = script;
    int next = 2;
    for (int index = 1; index < argc; ++index) {
        if (strncmp(argv[index], "-psn_", 5) != 0) arguments[next++] = argv[index];
    }
    arguments[next] = NULL;

    setenv("AMADEUS_PROJECT_ROOT", project_root, 1);
    execv(arguments[0], arguments);
    fprintf(stderr, "Unable to start Amadeus Wallpaper: %s\n", strerror(errno));
    free(arguments);
    return 1;
}

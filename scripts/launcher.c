#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <time.h>
#include <string.h>
#include <mach-o/dyld.h>

int main(int argc, char *argv[]) {
    char project_dir[1024] = {0};

    // 1. Try resolving relative to executable path:
    // .../Amadeus Wallpaper.app/Contents/MacOS/Amadeus Wallpaper -> .../../../..
    char exe_path[1024];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) == 0) {
        char *p = strstr(exe_path, "/Amadeus Wallpaper.app");
        if (p != NULL) {
            *p = '\0';
            char test_script[1024];
            snprintf(test_script, sizeof(test_script), "%s/scripts/start_wallpaper.sh", exe_path);
            if (access(test_script, X_OK) == 0) {
                strncpy(project_dir, exe_path, sizeof(project_dir) - 1);
            }
        }
    }

    // 2. Fallback to default user repository path
    if (project_dir[0] == '\0') {
        const char *fallback = "/Users/zjn/Desktop/data/code/Amadeus";
        char test_script[1024];
        snprintf(test_script, sizeof(test_script), "%s/scripts/start_wallpaper.sh", fallback);
        if (access(test_script, F_OK) == 0) {
            strncpy(project_dir, fallback, sizeof(project_dir) - 1);
        }
    }

    if (project_dir[0] == '\0') {
        fprintf(stderr, "[Launcher] Could not find Amadeus project root.\n");
        return 1;
    }

    // Prepare log redirection
    char logs_dir[1024];
    snprintf(logs_dir, sizeof(logs_dir), "%s/logs", project_dir);
    mkdir(logs_dir, 0755);

    char log_path[1024];
    snprintf(log_path, sizeof(log_path), "%s/logs/wallpaper_app.log", project_dir);

    int fd = open(log_path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) {
        dup2(fd, STDOUT_FILENO);
        dup2(fd, STDERR_FILENO);
        close(fd);
    }

    time_t now = time(NULL);
    printf("\n=== [Launcher] %s=== [Launcher] Starting Amadeus Wallpaper from %s\n", ctime(&now), project_dir);
    fflush(stdout);

    // Environment variables
    char python_path[1024];
    snprintf(python_path, sizeof(python_path), "%s/.venv/bin/python3", project_dir);
    setenv("NODE_ENV", "production", 1);
    setenv("AMADEUS_PYTHON", python_path, 1);

    const char *cur_path = getenv("PATH");
    char new_path[4096];
    snprintf(new_path, sizeof(new_path), "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:%s", cur_path ? cur_path : "");
    setenv("PATH", new_path, 1);

    // Clean up orphaned backend on port 17777 only if no active Electron instance is running
    if (system("pgrep -f 'Electron.*amadeus' >/dev/null 2>&1 || pgrep -f 'Electron.*electron' >/dev/null 2>&1") != 0) {
        system("kill $(lsof -nP -ti :17777 2>/dev/null) 2>/dev/null || true");
    }

    char electron_bin[1024];
    snprintf(electron_bin, sizeof(electron_bin), "%s/electron/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron", project_dir);

    char electron_dir[1024];
    snprintf(electron_dir, sizeof(electron_dir), "%s/electron", project_dir);

    if (access(electron_bin, X_OK) != 0) {
        fprintf(stderr, "[Launcher] Electron binary not found: %s\n", electron_bin);
        return 1;
    }

    // Build argument list: <electron_bin> <electron_dir> --wallpaper [args...]
    char **electron_args = malloc(sizeof(char *) * (argc + 5));
    electron_args[0] = electron_bin;
    electron_args[1] = electron_dir;
    electron_args[2] = "--wallpaper";
    int arg_idx = 3;
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "-psn_", 5) != 0) {
            electron_args[arg_idx++] = argv[i];
        }
    }
    electron_args[arg_idx] = NULL;

    // Change working directory to project root
    chdir(project_dir);

    execv(electron_bin, electron_args);

    perror("[Launcher] execv failed");
    return 1;
}

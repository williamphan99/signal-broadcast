/* Native entry point for Sudoku.app.
 *
 * Runs the app's Python inside this bundle's own process, so the Dock, the menu bar
 * and Activity Monitor show "Sudoku" rather than a separate Python app. The app's
 * code lives in Contents/Resources/app with its virtual environment beside it.
 */
#include <Python.h>
#include <dirent.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    char executable[PATH_MAX], resolved[PATH_MAX], app[PATH_MAX], lib[PATH_MAX], site[PATH_MAX] = "";
    uint32_t size = sizeof executable;
    if (_NSGetExecutablePath(executable, &size) != 0 || !realpath(executable, resolved)) return 70;
    snprintf(app, sizeof app, "%s/../Resources/app", dirname(resolved));
    if (chdir(app) != 0) return 71;
    snprintf(lib, sizeof lib, "%s/.venv/lib", app);
    DIR *directory = opendir(lib);
    for (struct dirent *entry; directory && (entry = readdir(directory));) {
        if (strncmp(entry->d_name, "python3.", 8) == 0)
            snprintf(site, sizeof site, "%s/%s/site-packages", lib, entry->d_name);
    }
    if (directory) closedir(directory);
    if (!site[0]) return 72;
    setenv("PYTHONPATH", site, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    /* python mac_app.py [arguments], dropping the -psn_ argument old macOS adds. */
    char **arguments = calloc((size_t)argc + 2, sizeof *arguments);
    if (!arguments) return 73;
    int count = 0;
    arguments[count++] = argv[0];
    arguments[count++] = "mac_app.py";
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "-psn_", 5) != 0 && strcmp(argv[i], "mac_app.py") != 0) arguments[count++] = argv[i];
    }
    return Py_BytesMain(count, arguments);
}

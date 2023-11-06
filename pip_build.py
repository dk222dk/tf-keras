"""Build the TF-Keras pip package.

The steps are as follows:

0. Run bazel build in TF-Keras root directory to obtain protobuf Python files.
1. Create a temporary build directory (e.g. `/tmp/keras_build`)
2. Copy the TF-Keras codebase to it (to `/tmp/keras_build/tf_keras/src`)
  and rewrite internal imports so that they refer to `keras.src` rather than
  just `keras`.
3. Also copy `setup.py` to the build directory.
4. List and import every file in codebase (in `/tmp/keras_build/tf_keras/src`),
  so we can inspect the symbols the codebase contains.
5. Use the annotations left by the `keras_export` decorator to filter the
  symbols that should be exported, as well as their export path (default one
  and v1 one).
6. Use this information to generate `__init__.py` files in
  `tmp/keras_build/tf_keras/`.
7. Run the setup script to write out build artifacts to `tmp/keras_build/dist`.
8. Copy the artifacts out. This is what should be uploaded to PyPI.

This script borrows heavily from Namex (https://github.com/fchollet/namex).

Notes:

* This script should be run on the TF-Keras codebase as obtained from GitHub
  (OSS-facing), not the Google-internal one. The files are expect to be already
  converted to their public form.
* This script only targets Linux x86 64. It could be adapted to MacOS
  relatively easily by changing requirements.txt and the bazel build script.
* This script should be run from an environment that has all TF-Keras
  dependencies installed. Note that their specific version is not important; the
  only thing that matters is that we should be able to import the TF-Keras
  codebase in its current state (so we can perform step 4). If you install the
  dependencies used by the latest TF-nightly you should be good.
"""

import argparse
import datetime
import glob
import importlib
import inspect
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

PACKAGE_NAME = "tf_keras"
DIST_DIRNAME = "dist"
SRC_DIRNAME = "src"
TMP_BUILD_DIRNAME = "keras_build"
TMP_TEST_DIRNAME = "keras_test"
VERBOSE = True
INIT_FILE_HEADER = """AUTOGENERATED. DO NOT EDIT."""
# These are symbols that have export issues and that we skip for now.
SYMBOLS_TO_SKIP = ["layer_test"]


def copy_keras_codebase(source_dir, target_dir):
    disallowed = [
        "tools",
        "integration_test",
    ]

    def ignore(path, names):
        to_ignore = []
        for name in names:
            if name.endswith("_test.py"):
                to_ignore.append(name)
            elif name in disallowed:
                to_ignore.append(name)
        return to_ignore

    shutil.copytree(source_dir, target_dir, ignore=ignore)


def convert_keras_imports(src_directory):
    def _convert_line(line):
        if (
            "import tf_keras.protobuf" in line
            or "from tf_keras.protobuf" in line
        ):
            return line
        # Imports starting from `root_name`.
        if line.strip() == f"import {PACKAGE_NAME}":
            line = line.replace(
                f"import {PACKAGE_NAME}",
                f"import {PACKAGE_NAME}.{SRC_DIRNAME} as {PACKAGE_NAME}",
            )
            return line

        line = line.replace(
            f"import {PACKAGE_NAME}.",
            f"import {PACKAGE_NAME}.{SRC_DIRNAME}.",
        )
        line = line.replace(
            f"from {PACKAGE_NAME}.",
            f"from {PACKAGE_NAME}.{SRC_DIRNAME}.",
        )
        line = line.replace(
            f"from {PACKAGE_NAME} import",
            f"from {PACKAGE_NAME}.{SRC_DIRNAME} import",
        )
        # Convert `import tf_keras as keras` into `import tf_keras.src as keras`
        line = line.replace(
            f"import {PACKAGE_NAME} as ",
            f"import {PACKAGE_NAME}.{SRC_DIRNAME} as ",
        )
        # A way to catch LazyLoader calls. Hacky.
        line = line.replace(
            'globals(), "tf_keras.', 'globals(), "tf_keras.src.'
        )
        return line

    for root, _, files in os.walk(src_directory):
        for fname in files:
            if fname.endswith(".py") and not fname.endswith("_pb2.py"):
                fpath = os.path.join(root, fname)
                if VERBOSE:
                    print(f"...processing {fpath}")
                with open(fpath) as f:
                    contents = f.read()
                lines = contents.split("\n")
                in_string = False
                new_lines = []
                for line in lines:
                    if line.strip().startswith('"""') or line.strip().endswith(
                        '"""'
                    ):
                        if line.count('"') % 2 == 1:
                            in_string = not in_string
                    else:
                        line = _convert_line(line)
                    new_lines.append(line)

                with open(fpath, "w") as f:
                    f.write("\n".join(new_lines) + "\n")


def generate_keras_api_files(package_directory, src_directory):
    if VERBOSE:
        print("# Compiling codebase entry points.")

    codebase_walk_entry_points = []
    for root, _, files in os.walk(src_directory):
        for fname in files:
            parts = root.split("/")
            parts = parts[parts.index("tf_keras") :]
            base_entry_point = ".".join(parts)
            if fname == "__init__.py":
                codebase_walk_entry_points.append(base_entry_point)
            elif fname.endswith(".py") and not fname.endswith("_test.py"):
                module_name = fname[:-3]
                codebase_walk_entry_points.append(
                    base_entry_point + "." + module_name
                )

    # Import all Python modules found in the code directory.
    modules = []
    sys.path.insert(0, os.getcwd())
    for entry_point in codebase_walk_entry_points:
        if VERBOSE:
            print(f"Load entry point: {entry_point}")
        mod = importlib.import_module(entry_point, package=".")
        modules.append(mod)

    if VERBOSE:
        print("# Compiling list of symbols to export.")

    # Populate list of all symbols to register.
    all_symbols = set()
    processed = set()
    from tensorflow.python.util import tf_decorator

    for module in modules:
        for name in dir(module):
            if name in SYMBOLS_TO_SKIP:
                continue
            symbol = getattr(module, name)

            # Get the real symbol behind any TF decorator
            try:
                _, symbol = tf_decorator.unwrap(symbol)
            except ModuleNotFoundError:
                # unwrap will not work on a ModuleSpec (which can't be
                # an API symbol anyway)
                continue

            # Skip if already seen
            if id(symbol) in processed:
                continue
            processed.add(id(symbol))

            try:
                if not hasattr(symbol, "_keras_api_names"):
                    continue
            except:  # noqa: E722
                if VERBOSE:
                    print(
                        f"[!] Could not inspect symbol '{name}' from {module}."
                    )
                continue
            # If the symbol is a non-registered subclass of
            # a registered symbol, skip it.
            skip = False

            def has_same_metadata(a, b):
                if (
                    hasattr(a, "_keras_api_names")
                    and hasattr(b, "_keras_api_names")
                    and a._keras_api_names == b._keras_api_names
                    and a._keras_api_names_v1 == b._keras_api_names_v1
                ):
                    return True
                return False

            try:
                classes = inspect.getmro(symbol)
                if len(classes) >= 2:
                    parents = classes[1:]
                    for p in parents:
                        if has_same_metadata(p, symbol):
                            skip = True
            except AttributeError:
                # getmro will error out on a non-class
                # (in which case there can be no subclassing issues).
                pass
            if not skip:
                all_symbols.add(symbol)

    # Generate __init__ files content.
    if VERBOSE:
        print("# Processing export path data for each symbol.")
    init_files_content = grab_symbol_metadata(all_symbols, is_v1=False)
    init_files_content_v1 = grab_symbol_metadata(all_symbols, is_v1=True)

    if VERBOSE:
        print("# Writing out API files.")
    write_out_api_files(
        init_files_content,
        target_dir=pathlib.Path(package_directory).parent.resolve(),
    )
    v1_path = os.path.join(package_directory, "api", "_v1")
    v2_path = os.path.join(package_directory, "api", "_v2")
    write_out_api_files(
        init_files_content,
        target_dir=v2_path,
        root_offset=["api", "_v2", "keras"],
    )
    write_out_api_files(
        init_files_content_v1,
        target_dir=v1_path,
        root_offset=["api", "_v1", "keras"],
    )
    # Add missing __init__ files in api dirs.
    with open(os.path.join(package_directory, "api", "__init__.py"), "w"):
        pass
    with open(os.path.join(v1_path, "__init__.py"), "w"):
        pass
    with open(os.path.join(v2_path, "__init__.py"), "w"):
        pass


def grab_symbol_metadata(all_symbols, is_v1=False):
    # init_files_content is a dict mapping a directory path to a list of
    # symbol metadata entries to populate the __init__ file for the directory.
    # Each entry is a dict with keys 'symbol' and 'export_name'.
    init_files_content = {}
    for symbol in all_symbols:
        if VERBOSE:
            print(f"...processing symbol '{symbol.__name__}'")
        if is_v1:
            api_names = symbol._keras_api_names_v1
        else:
            api_names = symbol._keras_api_names
        for export_path in api_names:
            export_modules = export_path.split(".")
            export_name = export_modules[-1]
            parent_path = os.path.join(*export_modules[:-1])
            if parent_path not in init_files_content:
                init_files_content[parent_path] = []
            init_files_content[parent_path].append(
                {"symbol": symbol, "export_name": export_name}
            )
            for i in range(1, len(export_modules[:-1])):
                intermediate_path = os.path.join(*export_modules[:i])
                if intermediate_path not in init_files_content:
                    init_files_content[intermediate_path] = []
                init_files_content[intermediate_path].append(
                    {
                        "module": export_modules[i],
                        "location": ".".join(export_modules[:i]),
                    }
                )
    return init_files_content


def write_out_api_files(init_files_content, target_dir, root_offset=None):
    # Go over init_files_content, make dirs,
    # create __init__.py file, populate file with public symbol imports.
    root_offset = root_offset or []
    for path, contents in init_files_content.items():
        # Use`tf_keras.<module>` format.
        module_path = path
        if path.startswith("keras"):
            module_path = "tf_" + module_path
        # Change pathnames from keras/layers -> tf_keras/layers unless
        # root_offset is explitly provided for API generation.
        if path.startswith("keras") and not root_offset:
            path = "tf_" + path
        os.makedirs(os.path.join(target_dir, path), exist_ok=True)
        init_file_lines = []
        modules_included = set()
        for symbol_metadata in contents:
            if "symbol" in symbol_metadata:
                symbol = symbol_metadata["symbol"]
                name = symbol_metadata["export_name"]
                if name == symbol.__name__:
                    init_file_lines.append(
                        f"from {symbol.__module__} import {symbol.__name__}"
                    )
                else:
                    init_file_lines.append(
                        f"from {symbol.__module__} "
                        f"import {symbol.__name__} as {name}"
                    )
            elif "module" in symbol_metadata:
                if symbol_metadata["module"] not in modules_included:
                    parts = module_path.split("/")
                    parts = [parts[0]] + root_offset + parts[1:]
                    module_location = ".".join(parts)
                    init_file_lines.append(
                        f"from {module_location} "
                        f"import {symbol_metadata['module']}"
                    )
                    modules_included.add(symbol_metadata["module"])

        init_path = os.path.join(target_dir, path, "__init__.py")
        if VERBOSE:
            print(f"...writing {init_path}")
        init_file_lines = sorted(init_file_lines)
        with open(init_path, "w") as f:
            contents = (
                f'"""{INIT_FILE_HEADER}"""\n\n'
                + "\n".join(init_file_lines)
                + "\n"
            )
            f.write(contents)


def build_pip_package(
    keras_root_directory,
    build_directory,
    package_directory,
    src_directory,
    dist_directory,
    is_nightly=False,
    rc=None,
):
    # Build TF-Keras with Bazel to get the protobuf .py files
    os.chdir(keras_root_directory)
    os.system(f"sh {os.path.join('tf_keras', 'tools', 'bazel_build.sh')}")
    os.chdir(build_directory)

    # Copy sources (`keras/` directory and setup files) to build directory
    copy_keras_codebase(
        os.path.join(keras_root_directory, "tf_keras"), src_directory
    )
    shutil.copy(
        os.path.join(keras_root_directory, "oss_setup.py"),
        os.path.join(build_directory, "setup.py"),
    )

    # Add blank __init__.py file at package root
    # to make the package directory importable.
    with open(os.path.join(package_directory, "__init__.py"), "w") as f:
        pass

    # Move protobuf .py files to package root.
    shutil.rmtree(os.path.join(src_directory, "protobuf"))
    shutil.move(
        os.path.join(keras_root_directory, "bazel-bin", "tf_keras", "protobuf"),
        package_directory,
    )
    # Add blank __init__.py file in protobuf dir.
    with open(
        os.path.join(package_directory, "protobuf", "__init__.py"), "w"
    ) as f:
        pass

    # Convert imports from `tf_keras.xyz` to `tf_keras.src.xyz`.
    convert_keras_imports(src_directory)

    # Generate API __init__.py files in `tf_keras/`
    generate_keras_api_files(package_directory, src_directory)

    # Make sure to export the __version__ string
    version = getattr(
        importlib.import_module("tf_keras.src", package="."), "__version__"
    )
    if is_nightly:
        date = datetime.datetime.now()
        version += f".dev{date.strftime('%Y%m%d%H')}"
    elif rc:
        version += rc
    with open(os.path.join(package_directory, "__init__.py")) as f:
        init_contents = f.read()
    with open(os.path.join(package_directory, "__init__.py"), "w") as f:
        f.write(init_contents + "\n\n" + f'__version__ = "{version}"\n')

    # Insert {{PACKAGE}} and {{VERSION}} strings in setup.py
    if is_nightly:
        package = PACKAGE_NAME + "-nightly"
    else:
        package = PACKAGE_NAME
    with open(os.path.join(build_directory, "setup.py")) as f:
        setup_contents = f.read()
    with open(os.path.join(build_directory, "setup.py"), "w") as f:
        setup_contents = setup_contents.replace("{{VERSION}}", version)
        setup_contents = setup_contents.replace("{{PACKAGE}}", package)
        f.write(setup_contents)

    # Build the package
    os.system("python3 -m build")

    # Save the dist files generated by the build process
    saved_filenames = []
    for filename in glob.glob(os.path.join(build_directory, "dist", "*.*")):
        if VERBOSE:
            print(f"Saving build artifact {filename}")
        shutil.copy(filename, dist_directory)
        saved_filenames.append(filename)
    if VERBOSE:
        print(f"Saved artifacts to {dist_directory}")
    return saved_filenames, version


def test_wheel(wheel_path, expected_version, requirements_path):
    test_directory = os.path.join(tempfile.gettempdir(), TMP_TEST_DIRNAME)
    os.mkdir(test_directory)
    os.chdir(test_directory)
    symbols_to_check = [
        "tf_keras.layers",
        "tf_keras.Input",
        "tf_keras.__internal__",
        "tf_keras.experimental",
    ]
    checks = ";".join(symbols_to_check)
    # Use Env var `TENSORFLOW_VERSION` for specific TF version to use in release
    # Uninstall `keras` after installing requirements
    # otherwise both will register `experimentalOptimizer` and test will fail.
    script = (
        "#!/bin/bash\n"
        "virtualenv kenv\n"
        f"source {os.path.join('kenv', 'bin', 'activate')}\n"
        f"pip3 install -r {requirements_path}\n"
        "pip3 uninstall -y tensorflow tf-nightly\n"
        'pip3 install "${TENSORFLOW_VERSION:-tf-nightly}"\n'
        "pip3 uninstall -y keras keras-nightly\n"
        f"pip3 install {wheel_path} --force-reinstall\n"
        f"python3 -c 'import tf_keras;{checks};print(tf_keras.__version__)'\n"
    )
    try:
        # Check version is correct
        output = subprocess.check_output(script.encode(), shell=True)
        output = output.decode().rstrip().split("\n")[-1].strip()
        if not output == expected_version:
            raise ValueError(
                "Incorrect version; expected "
                f"{expected_version} but received {output}"
            )
    finally:
        shutil.rmtree(test_directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--nightly",
        action="store_true",
        help="Whether this is for the `keras-nightly` package.",
    )
    parser.add_argument(
        "--RC",
        type=str,
        help="Whether this is for the release candidate.",
    )
    args = parser.parse_args()
    is_nightly = args.nightly
    rc = args.RC

    build_directory = os.path.join(tempfile.gettempdir(), TMP_BUILD_DIRNAME)
    keras_root_directory = pathlib.Path(__file__).parent.resolve()
    dist_directory = os.path.join(keras_root_directory, DIST_DIRNAME)
    package_directory = os.path.join(build_directory, PACKAGE_NAME)
    src_directory = os.path.join(build_directory, PACKAGE_NAME, SRC_DIRNAME)
    if VERBOSE:
        print(
            "Using:\n"
            f"build_directory={build_directory}\n"
            f"keras_root_directory={keras_root_directory}\n"
            f"dist_directory={dist_directory}\n"
            f"package_directory={package_directory}\n"
            f"src_directory={src_directory}\n"
            f"is_nightly={is_nightly}\n"
            f"rc={rc}"
        )
    if os.path.exists(build_directory):
        raise ValueError(f"Directory already exists: {build_directory}")
    os.mkdir(build_directory)
    os.mkdir(package_directory)
    if not os.path.exists(dist_directory):
        os.mkdir(dist_directory)
    try:
        saved_filenames, version = build_pip_package(
            keras_root_directory,
            build_directory,
            package_directory,
            src_directory,
            dist_directory,
            is_nightly,
            rc,
        )
        wheel_filename = [f for f in saved_filenames if f.endswith(".whl")][0]
        if VERBOSE:
            print("Testing wheel artifact.")
        test_wheel(
            wheel_path=os.path.join(dist_directory, wheel_filename),
            expected_version=version,
            requirements_path=os.path.join(
                keras_root_directory, "requirements.txt"
            ),
        )
        if VERBOSE:
            print("Test successful.")
    finally:
        # Clean up: remove the build directory (no longer needed)
        if VERBOSE:
            print(f"Deleting temp build directory at {build_directory}...")
        shutil.rmtree(build_directory)

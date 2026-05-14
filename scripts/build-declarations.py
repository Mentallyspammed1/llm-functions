#!/usr/bin/env python
# @describe Generate JSON declarations for tool/agent functions from script files.
#
# Parses Python docstrings and function signatures using AST to create a JSON array
# describing available functions, their descriptions, and parameters.
# This script supports various type hints and parameter formats commonly found in Python.
#
# Usage: ./build-declarations.py <script-file>
# Example: ./build-declarations.py tools/fs_ls.py

import ast
import os
import json
import re
import sys
from collections import OrderedDict
from typing import List, Dict, Any, Tuple

# The expected entry function name for tool scripts (e.g., 'run' in Python tools).
TOOL_ENTRY_FUNC = "run"


def main():
    """
    Main function to orchestrate script execution.
    Parses command-line arguments, reads the script file, extracts function declarations,
    and outputs the declarations in JSON format.
    """
    # Validate command-line arguments: Ensure a script file path is provided.
    if len(sys.argv) < 2:
        print("Usage: ./build-declarations.py <script-file>", file=sys.stderr)
        sys.exit(1)  # Exit with an error code if usage is incorrect.

    scriptfile = sys.argv[1]  # The path to the script file to process.
    # Determine if the script is a tool based on its directory. Tools are expected in the 'tools/' directory.
    is_tool = os.path.dirname(scriptfile) == "tools"

    try:
        # Read the script file content safely, handling potential file errors.
        with open(scriptfile, "r", encoding="utf-8") as f:
            contents = f.read()
    except FileNotFoundError:
        print(f"Error: File not found '{scriptfile}'", file=sys.stderr)
        sys.exit(1)  # Exit if the file cannot be found.
    except Exception as e:
        print(f"Error reading file '{scriptfile}': {e}", file=sys.stderr)
        sys.exit(1)  # Exit on other file reading errors.

    try:
        # First, try to extract declarations from header comments (@describe, @option)
        # This aligns with bash/js tool formats and is more robust.
        declarations = extract_from_comments(contents, scriptfile)

        if not declarations:
            # Fallback: extract functions, docstrings, and signatures from AST
            functions = extract_functions(contents, is_tool)

            # Iterate over each extracted function to build its declaration object.
            for func_name, docstring, func_args in functions:
                try:
                    # Parse the docstring to get the function's description and parameters.
                    description, params = parse_docstring(docstring)
                    # Skip functions that do not have a description.
                    if not description:
                        continue
                    # Build the JSON declaration object for the function.
                    declarations.append(
                        build_declaration(func_name, description, params, func_args)
                    )
                except ValueError as e:
                    print(
                        f"Error processing function '{func_name}' in '{scriptfile}': {e}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(
                        f"Unexpected error processing function '{func_name}' in '{scriptfile}': {e}",
                        file=sys.stderr,
                    )

    except SyntaxError as e:
        print(f"Syntax error parsing '{scriptfile}': {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error extracting functions from '{scriptfile}': {e}", file=sys.stderr)
        sys.exit(1)

    # Special handling for tool scripts:
    if is_tool and declarations:
        name = os.path.splitext(os.path.basename(scriptfile))[0]
        declarations[0]["name"] = name

    # Output the generated declarations as a JSON array, pretty-printed for readability.
    print(json.dumps(declarations, indent=2))


def extract_from_comments(contents: str, scriptfile: str) -> List[Dict[str, Any]]:
    """
    Extracts declarations from header comments like:
    # @describe Description
    # @option --param! <TYPE> Description
    """
    lines = contents.splitlines()
    description = ""
    properties = {}
    required = []

    for line in lines:
        if not line.startswith("#"):
            if description:  # Stop if we found a description but comments ended
                break
            continue

        line = line[1:].strip()
        if line.startswith("@describe"):
            description = line[len("@describe") :].strip()
        elif line.startswith("@option"):
            # @option --name! <TYPE> Description
            match = re.match(r"^@option\s+--([\w-]+)(!)?\s+(?:<(\w+)>)?\s*(.*)$", line)
            if match:
                opt_name = match.group(1).replace("-", "_")
                is_req = bool(match.group(2))
                opt_type = match.group(3) or "string"
                opt_desc = match.group(4).strip()

                type_map = {
                    "TEXT": "string",
                    "NUM": "integer",
                    "ENUM": "string",
                }
                json_type = type_map.get(opt_type.upper(), "string")

                properties[opt_name] = {"type": json_type, "description": opt_desc}
                if is_req:
                    required.append(opt_name)

    if not description:
        return []

    name = os.path.splitext(os.path.basename(scriptfile))[0]
    declaration = {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties},
    }
    if required:
        declaration["parameters"]["required"] = required

    return [declaration]


def extract_functions(
    contents: str, is_tool: bool
) -> List[Tuple[str, str, OrderedDict]]:
    """
    Extracts function definitions and their docstrings from Python source code using AST.
    Filters functions based on whether the script is a tool and if the function is private.

    Args:
        contents: The full source code of the Python script.
        is_tool: Boolean flag indicating if the script is a tool.

    Returns:
        A list of tuples, where each tuple contains: (function_name, docstring, ordered_dict_of_args_with_types).
    """
    tree = ast.parse(contents)  # Parse the Python code into an Abstract Syntax Tree.
    output = []  # List to store extracted function details.

    # Walk through all nodes in the AST.
    for node in ast.walk(tree):
        # Process only function definition nodes.
        if not isinstance(node, ast.FunctionDef):
            continue
        func_name = node.name  # Get the function name.

        # Skip private functions (starting with '_') or functions that do not match the tool entry point
        # if this is identified as a tool script.
        if (is_tool and func_name != TOOL_ENTRY_FUNC) or func_name.startswith("_"):
            continue

        docstring = (
            ast.get_docstring(node) or ""
        )  # Get the function's docstring, default to empty string if none.
        func_args = OrderedDict()  # Use OrderedDict to preserve the order of arguments.

        # Process positional arguments (args.args) and keyword-only arguments (args.kwonlyargs).
        # Note: More complex signatures (vararg, kwarg) are not fully handled here for brevity.
        for arg in node.args.args:
            arg_name = arg.arg
            arg_type = get_arg_type(arg.annotation)  # Infer type hint string.
            func_args[arg_name] = arg_type

        # Add support for keyword-only arguments if needed:
        # for arg in node.args.kwonlyargs:
        #     arg_name = arg.arg
        #     arg_type = get_arg_type(arg.annotation)
        #     func_args[arg_name] = arg_type

        output.append(
            (func_name, docstring, func_args)
        )  # Append extracted info to the output list.
    return output


def get_arg_type(annotation) -> str:
    """
    Infers a string representation of an argument's type hint from its AST node.
    Supports basic types, List, Optional, and Literal.

    Args:
        annotation: The AST node representing the type annotation.

    Returns:
        A string representing the type hint (e.g., "str", "list[int]", "Optional[str]?", "str|int").
    """
    if annotation is None:
        return "any"  # Default to 'any' if no type hint is provided.
    elif isinstance(annotation, ast.Name):
        # Handles simple type names like 'str', 'int', 'bool'.
        return annotation.id
    elif isinstance(annotation, ast.Subscript):
        # Handles generic types like List[str], Optional[str], Literal['a', 'b'].
        if isinstance(annotation.value, ast.Name):
            type_name = annotation.value.id
            if type_name == "List":
                # Handle List[<type>]. Recursively get the type of elements.
                child_type = get_arg_type(annotation.slice)
                return f"list[{child_type}]"
            elif type_name == "Literal":
                # Handle Literal['choice1', 'choice2']. Extract literal values.
                if isinstance(annotation.slice, ast.Tuple):
                    # Extract values from a tuple Literal (e.g., Literal['a', 'b']).
                    literals = [
                        ast.unparse(el).strip("'") for el in annotation.slice.elts
                    ]
                    return "|".join(literals)
                else:  # Handle single literal case (e.g., Literal['a']).
                    return ast.unparse(annotation.slice).strip("'")
            elif type_name == "Optional":
                # Handle Optional[<type>]. Append '?' to indicate optionality.
                child_type = get_arg_type(annotation.slice)
                return f"{child_type}?"
    # Fallback for other types or if parsing fails. Use ast.unparse for a string representation.
    try:
        return ast.unparse(annotation)
    except Exception:
        return "any"  # Default to 'any' if unparsing fails.


def parse_docstring(docstring: str) -> Tuple[str, Dict[str, Tuple[str, str]]]:
    """
    Parses a Python docstring to extract the main description and parameter details.
    Expects parameters to be documented in an "Args:" section, formatted like:
    `name: type - description` or `[name]: type - description`.

    Args:
        docstring: The docstring content.

    Returns:
        A tuple containing: (description_string, dictionary_of_params).
        The dictionary maps parameter names to tuples of (type_string, description_string).
    """
    lines = docstring.splitlines()
    description = ""
    raw_params: List[str] = []
    current_section = ""  # Tracks whether we are parsing 'description' or 'args'.

    for line in lines:
        stripped_line = line.strip()
        # Identify section headers like "Args:", "Returns:", etc.
        if stripped_line.startswith("Args:"):
            current_section = "args"
            continue
        elif stripped_line.startswith("Returns:") or stripped_line.startswith(
            "Raises:"
        ):
            # Stop processing 'args' section if another section header is encountered.
            break
        elif current_section == "args":
            # Collect lines that look like parameter descriptions:
            # They typically start with indentation followed by a name and colon, or hyphenated list items.
            if re.match(r"^\s+\S+:", stripped_line) or re.match(
                r"^\s+- \S+", stripped_line
            ):
                raw_params.append(stripped_line)
            elif stripped_line:  # If it's not indented but non-empty, assume it's a new section or trailing text.
                break
        elif current_section == "description":
            # Accumulate lines for the main description.
            if stripped_line:
                description += f"\n{stripped_line}"
        else:  # Default state: parse lines as part of the description until a section is found.
            if stripped_line:
                description += f"\n{stripped_line}"

    params = {}
    # Parse each collected raw parameter description into structured data.
    for raw_param in raw_params:
        try:
            name, type_, param_description = parse_param(raw_param)
            params[name] = (type_, param_description)
        except ValueError as e:
            # Re-raise errors with context about the function and parameter for better debugging.
            raise ValueError(f"Invalid parameter format: '{raw_param}'. Details: {e}")

    return (
        description.strip(),
        params,
    )  # Return the cleaned description and parsed parameters.


def parse_param(raw_param: str) -> Tuple[str, str, str]:
    """
    Parses a single parameter line from a docstring (e.g., 'name: type - description').
    Extracts name, type, and description, handling optional parameters.

    Args:
        raw_param: The raw string for a parameter line.

    Returns:
        A tuple: (parameter_name, parameter_type_string, parameter_description_string).

    Raises:
        ValueError: If the format of the raw_param string does not match expectations.
    """
    # Regex to capture: {type} [name] - description
    # - {type}: mandatory, captured in group 1.
    # - [name]: optional name (enclosed in brackets), captured in group 2. Handles non-space chars.
    # - - description: optional description, captured in group 3. Allows spaces after hyphen or just space.
    match = re.match(r"^{([^}]+)}\s*(\S+?)(?:(?: *- +| +)(\S.*))?$", raw_param)

    if not match:
        raise ValueError(
            f"Expected format like '{{type}} name - description' or '{{type}} [name] - description', but got '{raw_param}'"
        )

    type_full = match.group(1)  # The full type string from docstring.
    name = match.group(2)  # The parameter name.
    description = match.group(3) or ""  # The parameter description, defaults to empty.

    # Check if the name is optional (enclosed in brackets).
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]  # Remove brackets to get the actual parameter name.

    # Map the extracted type string (potentially with '?' or list notation) to a JSON schema type.
    json_type = map_python_type_to_json_schema(type_full)

    return (name, json_type, description.strip())  # Return extracted data.


def map_python_type_to_json_schema(type_: str) -> str:
    """
    Maps common Python type hints (from docstrings or type annotations) to their JSON Schema equivalents.

    Args:
        type_: The type string from Python (e.g., "str", "list[int]", "Optional[str]", "str|int").

    Returns:
        A string representing the JSON Schema type (e.g., "string", "array", "string?").
    """
    type_lower = (
        type_.lower()
    )  # Normalize to lowercase for case-insensitive comparison.
    if type_lower == "str":
        return "string"
    elif type_lower == "int":
        return "integer"
    elif type_lower == "float":
        return "number"
    elif (
        type_ == "bool"
    ):  # Check original case for 'bool' if case-insensitive check fails for boolean
        return "boolean"
    elif type_ == "any":
        return "string"  # Default unknown types to string for broader compatibility.
    elif type_.startswith("list[") and type_.endswith("]"):
        # Basic handling for list[<type>]. E.g., "list[str]" becomes "array".
        return "array"
    # Handle '|' for unions/enums. For simplicity, map to string with enum values if possible.
    elif "|" in type_:
        # If it's a union, try to resolve to string with enum.
        return "string"  # Treat unions as string, potentially with enum values derived later if needed.
    else:
        # For unhandled types, default to string or raise a warning.
        # print(f"Warning: Unsupported Python type hint '{type_}'. Defaulting to 'string'.", file=sys.stderr)
        return "string"  # Default for unknown types.


def build_declaration(
    name: str,
    description: str,
    params: Dict[str, Tuple[str, str]],
    args_types: OrderedDict[str, str],
) -> Dict[str, Any]:
    """
    Builds a JSON declaration object for a function.
    Combines information from AST (argument names and hints) and docstring parsing (parameter details).

    Args:
        name: The name of the function.
        description: The function's main description.
        params: A dictionary of parsed parameters from the docstring {param_name: (type_str, desc_str)}.
        args_types: An OrderedDict of function arguments with their type hints from AST.

    Returns:
        A dictionary representing the JSON declaration object.
    """
    declaration = {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {},  # Schema properties will be added here.
        },
    }
    schema = declaration["parameters"]  # Shortcut reference to the schema part.
    required_params = []  # List to store names of required parameters.

    # Iterate through arguments defined in the function signature first to maintain order.
    # Then, enrich with details from parsed docstring parameters if available.
    for arg_name, arg_hint_type in args_types.items():
        param_info = params.get(
            arg_name
        )  # Get details from docstring parsing for this argument.
        param_type = arg_hint_type  # Start with the type hint from AST.
        param_description = ""
        is_required = True  # Assume required by default.

        if param_info:
            # If docstring provides details, use them preferentially.
            parsed_type, parsed_description = param_info
            if parsed_type.endswith("?"):
                is_required = False
                param_type = parsed_type[:-1]  # Remove '?' from type string.
            else:
                param_type = parsed_type  # Use docstring type if not marked optional.
            param_description = parsed_description

        # If type is still 'any' or empty after considering AST hint, use docstring type or default to 'string'.
        if not param_type or param_type == "any":
            if (
                param_info
                and params.get(arg_name)
                and params.get(arg_name)[0]
                and params.get(arg_name)[0] != "any"
            ):
                # Use type from docstring if available and not 'any'.
                type_from_doc = params.get(arg_name)[0]
                if type_from_doc.endswith("?"):
                    is_required = False
                    param_type = type_from_doc[:-1]
                else:
                    param_type = type_from_doc
            else:
                param_type = "string"  # Default to string if no type info found.

        # Build the JSON schema property object using the mapped type and description.
        try:
            property_schema = build_property(param_type, param_description)
        except ValueError as e:
            # Propagate error if property schema cannot be built, with context.
            raise ValueError(f"Error building property for argument '{arg_name}': {e}")

        schema["properties"][arg_name] = (
            property_schema  # Add the generated schema to properties.
        )
        if is_required:
            required_params.append(
                arg_name
            )  # Add to required list if parameter is mandatory.

    # Add the 'required' array to the schema if there are any required parameters.
    if required_params:
        schema["required"] = required_params

    return declaration  # Return the complete declaration object.


def build_property(type_: str, description: str) -> Dict[str, Any]:
    """
    Builds a JSON schema property object from a parsed type string and description.
    Handles basic types, arrays, and enums.

    Args:
        type_: The mapped type string (e.g., "string", "integer", "array", "string[]", "str|int").
        description: The description for the property.

    Returns:
        A dictionary representing the JSON schema property.
    """
    property_schema = {}
    if type_ == "string":
        property_schema["type"] = "string"
    elif type_ == "integer":
        property_schema["type"] = "integer"
    elif type_ == "number":
        property_schema["type"] = "number"
    elif type_ == "boolean":
        property_schema["type"] = "boolean"
    elif type_ == "array":
        property_schema["type"] = "array"
        # Assume array items are strings if not specified further. A more complex system could parse `list[type]` more deeply.
        property_schema["items"] = {"type": "string"}
    elif type_.startswith("list[") and type_.endswith("]"):
        # Basic handling for list[<type>]. Extract type inside brackets.
        property_schema["type"] = "array"
        item_type = type_[5:-1]  # e.g., "str" from "list[str]"
        # Recursively map item type if needed, or default.
        property_schema["items"] = {"type": map_python_type_to_json_schema(item_type)}
    else:
        # For unhandled types, default to string or raise an error for clarity.
        # print(f"Warning: Unsupported type mapping '{type_}'. Defaulting to 'string'.", file=sys.stderr)
        property_schema["type"] = "string"  # Default for unknown types.

    property_schema["description"] = description  # Add the description to the schema.
    return property_schema


if __name__ == "__main__":
    main()

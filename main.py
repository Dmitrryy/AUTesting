from openai import OpenAI
import logging
import re
import uuid
import os
import subprocess


import AUTesting.PGenerator as pgen
import AUTesting.parser as aup
import AUTesting.compiler as compiler

import argparse

# coverage:
# lcov --capture --directory build/ --output-file build/coverage.info
# genhtml build/coverage.info --output-directory out

#example run: python3 main.py --source-file="./examples/RBTree/RBTree.c" --include-file="./examples/RBTree/RBTree.h" --compiler=gcc --model-gpt=gpt-3.5-turbo

def extract_c_functions(header_content):
    """
    Extracts C function declarations from the given header file content.

    Args:
    - header_content (str): The content of the C header file.

    Returns:
    - List[str]: A list of function declarations.
    """
    # Regular expression for C function declarations
    # This regex tries to capture typical C function declarations, including those with pointers and nested parentheses.
    function_pattern = re.compile(
        r"\b[A-Za-z_][A-Za-z0-9_]*[\s\*]+\**\s*[A-Za-z_][A-Za-z0-9_]*\s*\((?:[^()]|\([^()]*\))*\)"
    )

    # Find all matches
    return function_pattern.findall(header_content)


def remove_c_comments(code):
    """
    Removes C-style comments from a string of C code.

    Args:
    - code (str): The string containing C code.

    Returns:
    - str: The C code string with comments removed.
    """
    # Pattern to match single-line and multi-line comments
    pattern = re.compile(r"//.*?$|/\*.*?\*/", re.DOTALL | re.MULTILINE)

    # Remove the comments
    clean_code = re.sub(pattern, "", code)
    clean_code = os.linesep.join([s for s in clean_code.splitlines() if s])
    return clean_code

def parseArguments():
    parser = argparse.ArgumentParser(description = 'Generator UnitTests for C/C++ code')
    parser.add_argument("--source-file", help="path to file with sources", required=True)
    parser.add_argument("--include-file", help="path to include file", required=True)
    parser.add_argument("--compiler", help="Using compiler", default="gcc")
    parser.add_argument("--model-gpt", help="Using version of chatGPT for generation tests, gpt-4-1106-preview-recommend for the best results", default="gpt-4-1106-preview")
    return parser.parse_args()


if __name__ == "__main__":
    args = parseArguments()
    logging.basicConfig(level=logging.DEBUG)
    logging.debug("Hello world!")

    include_to_test = args.include_file
    includes = args.include_file
    sources = args.source_file

    with open(include_to_test, "r") as header:
        content = header.read()
        content = remove_c_comments(content)
        functions = extract_c_functions(content)

    signatures_num = len(functions)
    logging.info(f"Signatures num: {signatures_num}")
    logging.info(f"Signatures: {functions}")

    # test first function
    # func = func_s[0]
    prompts = []
    for sig in functions:
        prompts.extend(pgen.generate(sig))

    # FIXME: multi file project?
    prompts_str = []
    with open(sources, "r") as src:
        content = src.read()
        content = remove_c_comments(content)
        for pr in prompts:
            header = f"I have header '{include_to_test}' with all function prototypes. C code with functions definitions: {content}\n."
            prompts_str.append(header + pr.generate())

    # use LLM to generate tests
    client = OpenAI()

    # generate initial chats
    messages_s = []
    for prompt in prompts_str:
        messages = [
            {
                "role": "system",
                "content": "You are a professional tester of C programs. When I ask you to write a test, you will answer only in code without any explanatory text. Response should not contain tested code. Use only asserts for testing. Test should contain main function.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]
        messages_s.append(messages)

    for prompt in messages_s:
        logging.info(f"Prompt: {prompt}")
        completion = client.chat.completions.create(
            model=args.model_gpt, messages=prompt
        )
        logging.info(f"Response: {completion}")
        prompt.append(
            {"role": "assistant", "content": completion.choices[0].message.content}
        )
        # break

    # NOTE: assume that there is only once code section in a response
    generated = 0
    compiled = []
    passed = []
    failed = []
    for compl in messages_s:
        if len(compl) <= 2:
            continue

        test = aup.extract_code_from_chatgpt_response(compl[-1]["content"])
        if len(test) == 0:
            test = compl[-1]["content"]
        else:
            test = test[0]

        logging.info(f"Tests:")

        test_src = "./build/" + str(uuid.uuid4()) + ".c"
        test_out = test_src + ".out"
        with open(test_src, "w") as cpp:
            code = "/* file autogenerated */" + compiler.fixErrors(test)
            print(code, file=cpp)
        logging.info(f"--------------------------------------------------")
        logging.info(f"  Test:\n{test}")
        logging.info(f"Launch compiler")
        stat = compiler.Compiler(test_src, include_file=includes, using_compiler=args.compiler).run(sources, test_out)

        logging.info(f"Compiler result: {stat}")
        if stat.returncode == 0:
            compiled.append(test_out)
            command_line = f"{test_out}"
            stat = subprocess.run(command_line, capture_output=True, text=True)
            logging.info(f"Run result: {stat}")
            is_passed = stat.returncode == 0
            if stat.returncode == 0:
                passed.append(test_out)
            else:
                failed.append(test_out)
        else:
            compl.append(
                {
                    "role": "user",
                    "content": f"Compilation of tests above failed with error: {stat.stderr}. Generate fixed test.",
                }
            )
            logging.info(f"Recompile prompt: {compl}")
            compl = client.chat.completions.create(
                model=args.model_gpt, messages=compl
            )
            logging.info(f"Response: {compl}")

            test = aup.extract_code_from_chatgpt_response(
                compl.choices[0].message.content
            )
            if len(test) == 0:
                test = compl.choices[0].message.content
            else:
                test = test[0]

            with open(test_src, "w") as cpp:
                code = "/* file re-autogenerated */" + compiler.fixErrors(test)
                print(code, file=cpp)
            stat = compiler.Compiler(test_src, include_file=includes, using_compiler=args.compiler).run(
                sources, test_out
            )
            logging.info(f"Compiler result: {stat}")
            if stat.returncode == 0:
                compiled.append(test_out)
                command_line = f"{test_out}"
                stat = subprocess.run(command_line, capture_output=True, text=True)
                logging.info(f"Run result: {stat}")
                is_passed = stat.returncode == 0
                if stat.returncode == 0:
                    passed.append(test_out)
                else:
                    failed.append(test_out)

    logging.info("=-----------------------------------------------")
    logging.info("Stats:")
    logging.info(f"Generated: {generated}")
    logging.info(f"Compiled ({len(compiled)}): {compiled}")
    logging.info(f"Passed ({len(passed)}): {passed}")
    logging.info(f"Failed ({len(failed)}): {failed}")

#!/usr/bin/env python3
"""
Transform test_server.py using AST manipulation.

Uses Python's ast module to parse, transform, and unparse.
"""

import ast
import re
from pathlib import Path


class MockAppTransformer(ast.NodeTransformer):
    """Transform async with mock_app(...) as f: blocks to use fixture."""

    def __init__(self):
        self.import_changed = False
        # Track function names that need mock_app param
        self.funcs_with_mock_app = set()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:
        """Change 'from tests.conftest import mock_app' to import AppFixture."""
        if node.module == 'tests.conftest' and not self.import_changed:
            new_names = []
            for alias in node.names:
                if alias.name == 'mock_app':
                    new_names.append(ast.alias(name='AppFixture', asname=None))
                else:
                    new_names.append(alias)
            if any(a.name == 'AppFixture' for a in new_names):
                self.import_changed = True
                return ast.ImportFrom(
                    module=node.module,
                    names=new_names,
                    level=node.level
                )
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        """Transform functions that contain mock_app async with."""
        # Check if this is a test function with mock_app
        new_body = []
        mock_app_blocks = []  # list of (with_node, args_list, var_name)
        
        for stmt in node.body:
            if isinstance(stmt, ast.AsyncWith):
                # Check if this is mock_app(...)
                if self._is_mock_app_with(stmt):
                    args = self._extract_mock_app_args(stmt)
                    var_name = self._extract_var_name(stmt)
                    if args is not None and var_name is not None:
                        mock_app_blocks.append((stmt, args, var_name))
                        # Lift the body (replace f. with mock_app.)
                        for body_stmt in stmt.body:
                            new_body.append(self._replace_var_refs(body_stmt, var_name))
                        continue
            new_body.append(stmt)
        
        if mock_app_blocks:
            # Add mock_app param to function
            new_args = list(node.args.args)
            # Check if mock_app already in args
            has_mock_app = any(a.arg == 'mock_app' for a in new_args)
            if not has_mock_app:
                mock_app_arg = ast.arg(
                    arg='mock_app',
                    annotation=ast.Name(id='AppFixture', ctx=ast.Load())
                )
                new_args.append(mock_app_arg)
            
            node.args.args = new_args
            
            # Add parametrize decorator for the first block's args
            # (If there are multiple blocks, this is incomplete)
            if len(mock_app_blocks) == 1:
                args_dict = mock_app_blocks[0][1]
                if args_dict:
                    # Build parametrize decorator
                    # Convert args dict to keyword list
                    keys = []
                    values = []
                    for kw in args_dict:
                        keys.append(ast.Constant(value=kw.arg))
                        values.append(kw.value)
                    
                    dict_node = ast.Dict(keys=keys, values=values)
                    list_node = ast.List(elts=[dict_node], ctx=ast.Load())
                    
                    # Build: @pytest.mark.parametrize('mock_app', [...], indirect=True)
                    parametrize_call = ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(
                                value=ast.Name(id='pytest', ctx=ast.Load()),
                                attr='mark',
                                ctx=ast.Load()
                            ),
                            attr='parametrize',
                            ctx=ast.Load()
                        ),
                        args=[
                            ast.Constant(value='mock_app'),
                            list_node,
                        ],
                        keywords=[
                            ast.keyword(arg='indirect', value=ast.Constant(value=True))
                        ]
                    )
                    node.decorator_list.append(parametrize_call)
            
            node.body = new_body
        
        # Continue visiting children
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        """Same as FunctionDef but for async functions."""
        new_body = []
        mock_app_blocks = []
        
        for stmt in node.body:
            if isinstance(stmt, ast.AsyncWith):
                if self._is_mock_app_with(stmt):
                    args = self._extract_mock_app_args(stmt)
                    var_name = self._extract_var_name(stmt)
                    if args is not None and var_name is not None:
                        mock_app_blocks.append((stmt, args, var_name))
                        for body_stmt in stmt.body:
                            new_body.append(self._replace_var_refs(body_stmt, var_name))
                        continue
            new_body.append(stmt)
        
        if mock_app_blocks:
            new_args = list(node.args.args)
            has_mock_app = any(a.arg == 'mock_app' for a in new_args)
            if not has_mock_app:
                mock_app_arg = ast.arg(
                    arg='mock_app',
                    annotation=ast.Name(id='AppFixture', ctx=ast.Load())
                )
                new_args.append(mock_app_arg)
            
            node.args.args = new_args
            
            if len(mock_app_blocks) == 1:
                args_list = mock_app_blocks[0][1]
                if args_list:
                    keys = []
                    values = []
                    for kw in args_list:
                        keys.append(ast.Constant(value=kw.arg))
                        values.append(kw.value)
                    
                    dict_node = ast.Dict(keys=keys, values=values)
                    list_node = ast.List(elts=[dict_node], ctx=ast.Load())
                    
                    parametrize_call = ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(
                                value=ast.Name(id='pytest', ctx=ast.Load()),
                                attr='mark',
                                ctx=ast.Load()
                            ),
                            attr='parametrize',
                            ctx=ast.Load()
                        ),
                        args=[
                            ast.Constant(value='mock_app'),
                            list_node,
                        ],
                        keywords=[
                            ast.keyword(arg='indirect', value=ast.Constant(value=True))
                        ]
                    )
                    node.decorator_list.append(parametrize_call)
            
            node.body = new_body
        
        return self.generic_visit(node)

    def _is_mock_app_with(self, node: ast.AsyncWith) -> bool:
        """Check if this AsyncWith is mock_app(...)."""
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                func = item.context_expr.func
                if isinstance(func, ast.Name) and func.id == 'mock_app':
                    return True
        return False

    def _extract_mock_app_args(self, node: ast.AsyncWith) -> list | None:
        """Extract keyword arguments from mock_app(...) call."""
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                return item.context_expr.keywords
        return None

    def _extract_var_name(self, node: ast.AsyncWith) -> str | None:
        """Extract the variable name from 'as f'."""
        for item in node.items:
            if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                return item.optional_vars.id
        return None

    def _replace_var_refs(self, node: ast.AST, var_name: str) -> ast.AST:
        """Replace f.client with mock_app.client etc."""
        class VarReplacer(ast.NodeTransformer):
            def visit_Attribute(self, n: ast.Attribute):
                if isinstance(n.value, ast.Name) and n.value.id == var_name:
                    return ast.Attribute(
                        value=ast.Name(id='mock_app', ctx=ast.Load()),
                        attr=n.attr,
                        ctx=n.ctx
                    )
                return self.generic_visit(n)
        
        return VarReplacer().visit(node)


def transform_file(path: str) -> None:
    """Parse, transform, unparse."""
    text = Path(path).read_text()
    
    # Parse
    tree = ast.parse(text)
    
    # Transform
    transformer = MockAppTransformer()
    new_tree = transformer.visit(tree)
    ast.fix_missing_locations(new_tree)
    
    # Unparse
    new_text = ast.unparse(new_tree)
    
    # Write
    Path(path).write_text(new_text)
    print("AST transformation complete.")


if __name__ == '__main__':
    transform_file('tests/chat/test_server.py')
    # Verify
    import subprocess
    r = subprocess.run(['python3', '-c', 
        "import ast; ast.parse(open('tests/chat/test_server.py').read()); print('OK')"],
        capture_output=True, text=True)
    print('stdout:', r.stdout)
    if r.stderr:
        print('stderr:', r.stderr)

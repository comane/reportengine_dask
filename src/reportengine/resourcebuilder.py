# -*- coding: utf-8 -*-
"""
Created on Fri Nov 13 21:18:06 2015

@author: zah
"""
from __future__ import generator_stop

from collections import namedtuple, Sequence, defaultdict, OrderedDict
import logging
import inspect
import functools
import signal

import curio

from reportengine import dag
from reportengine import namespaces
from reportengine.configparser import InputNotFoundError
from reportengine.checks import CheckError
from reportengine.utils import ChainMap

log = logging.getLogger(__name__)

RESOURCE = "resource"
PROVIDER = "provider"

Target = namedtuple('Target', ('name', 'nsspec', 'extraargs'))


#TODO: Fix help system for collect
class collect:
    def __init__(self, function, fuzzyspec):
        self.fuzzyspec = fuzzyspec
        self.function = function

    def __call__(self):
        return list(self.result.values())

def add_to_dict_flag(result, origin, target, index):
    target.function.result[index] = result

async def _async_identity(f, *args, **kwargs):
    return f(*args, **kwargs)

class provider:
    """Decorator intended to be used for the functions that are to
    be exposed as providers, either directly or trough more specialized
    decorators in reportengine."""
    def __init__(self, f):
        functools.update_wrapper(self, f)
        self.f = f

    def __call__(self, *args, **kwargs):
        return self.f(*args, **kwargs)

CallSpec = namedtuple('CallSpec', ('function', 'kwargs', 'resultname',
                                   'nsspec'))

CollectSpec = namedtuple('CollectSpec', ('function', 'kwargs', 'resultname',
                                   'nsspec'))


#TODO; Improve namespace spec
def print_callspec(spec, nsname = None):

    if nsname is None:
        res = spec.resultname
    else:
        res = "nsname[{!r}]".format(spec.resultname)
    callargs = ', '.join("%s=%s"% (kw, kw) for kw in spec.kwargs)
    try:
        f = spec.function.__qualname__

    #functools.partial annoyingly doesn't wrap
    except AttributeError:
        f = spec.function.func.__qualname__
        callargs += ", " + ", ".join("%s=%s" % (kw,val) for
                                     kw,val in spec.function.keywords.items())

    return "{res} = {f}({callargs})".format(res=res,
                                        f=f,
                                        callargs=callargs)

CallSpec.__str__ = print_callspec

class ResourceExecutor():

    def __init__(self, graph, rootns, environment=None):
        self.graph = graph
        self.rootns = rootns
        self.environment = environment

    def resolve_callargs(self, callspec):
        function, kwargs, resultname, nsspec = callspec
        namespace = namespaces.resolve(self.rootns, nsspec)
        kwdict = {kw: namespace[kw] for kw in kwargs}
        if hasattr(function, 'prepare'):
            prepare_args = function.prepare(spec=callspec,
                                            namespace=namespace,
                                            environment=self.environment,)
        else:
            prepare_args = {}

        return kwdict, prepare_args

    def execute_sequential(self):
        for node in self.graph:
            callspec = node.value
            if isinstance(callspec, CollectSpec):
                result = callspec.function()
            else:
                result = self.get_result(callspec.function,
                                         *self.resolve_callargs(callspec))
            self.set_result(result, callspec)

    #This needs to be a staticmethod, because otherwise we have to serialize
    #the whole self object when passing to multiprocessing.
    @staticmethod
    def get_result(function, kwdict, prepare_args):
        fres =  function(**kwdict)
        if hasattr(function, 'final_action'):
            return function.final_action(fres, **prepare_args)
        return fres

    def set_result(self, result, spec):
        function, _, resultname, nsspec = spec
        namespace = namespaces.resolve(self.rootns, nsspec)
        put_map = namespace.maps[1]
        log.debug("Setting result for %s %s", spec, nsspec)

        if resultname in put_map:
            raise ValueError("Resource already set: %s" % resultname)
        put_map[resultname] = result

        for action, args in self._node_flags[spec]:
            action(result, spec, **dict(args))



    async def _run_parallel(self, deps, completed_spec):
        try:
            runnable_specs = deps.send(completed_spec)
        except StopIteration:
            return
        pending_tasks = {}
        for pending_spec in runnable_specs:
            if isinstance(pending_spec, CollectSpec):
                remote_coro = _async_identity(pending_spec.function)
            else:
                remote_coro = curio.run_in_process(self.get_result,
                                       pending_spec.function,
                                       *self.resolve_callargs(pending_spec))
            pending_task = await curio.spawn(remote_coro)
            pending_tasks[pending_task] = pending_spec

        next_runs =  []
        waiter = curio.wait(pending_tasks)
        async for completed_task in waiter:
            try:
                result = await completed_task.join()
            except curio.TaskError as e:
                raise curio.KernelExit() from e

            new_completed_spec = pending_tasks.pop(completed_task)
            self.set_result(result, new_completed_spec)
            next_runs_coro = self._run_parallel(deps, new_completed_spec)
            next_runs.append(await curio.spawn(next_runs_coro))

        for wait_run in next_runs:
            await wait_run.join()



    def execute_parallel(self):
        deps = self.graph.dependency_resolver()
        #https://github.com/dabeaz/curio/issues/72
        kernel = curio.Kernel()
        async def kb_interrupt():
            await curio.SignalSet(signal.SIGINT).wait()
            raise KeyboardInterrupt()
        async def main():
            await curio.spawn(kb_interrupt(), daemon=True)
            await self._run_parallel(deps, None)

        with kernel:
            try:
                kernel.run(main())
            except KeyboardInterrupt:
                log.info("Canceling remaining tasks")
                raise
            except curio.KernelExit as e:
                raise e.__cause__

    def __str__(self):
        return "\n".join(print_callspec(node.value) for node in self.graph)


class ResourceError(Exception):
    def __init__(self, name, message, parents):
        self.name = name
        self.message = message
        if not parents:
            parents = ('Target specification',)
        self.parents = parents

    def __str__(self):
        return "Could not process the resource '%s', required by:\n%s\n%s"%(
                self.name, '\n'.join(' - ' + p for p in self.parents),
                self.message)

class ResourceNotUnderstood(ResourceError, TypeError): pass



EMPTY = inspect.Signature.empty




class ResourceBuilder(ResourceExecutor):

    def __init__(self, input_parser, providers, targets, environment=None):

        self.input_parser = input_parser

        if not isinstance(providers, Sequence):
            providers = [providers]
        self.providers = providers
        self.targets = targets

        self.rootns = ChainMap()
        self.graph = dag.DAG()

        self.environment = environment

        self._node_flags = defaultdict(lambda: set())

    def is_provider_func(self, name):
        return any(hasattr(provider, name) for provider in self.providers)

    def get_provider_func(self, name):
        for provider in self.providers:
            func = getattr(provider, name, False)
            if func:
                return func
        raise AttributeError("No such provider function: %s" % name)

    def explain_provider(self, provider_name):
        """Collect the dependencies of a given provider name, by analizing
        the function signature.
        The result is a list where the first element is the provider function.
        The next elements are tuples of the form ``(type, name, value)`` where

         - ``type`` is one of ``('provider', 'config', 'unknown')``
         - ``name`` is the name of the dpenendncy
         - ``value`` depends on the type:
          - For providers, it is the output of ``self.explain_provider(name)``.
          - For config, it is the output of ``self.input_parser.explain_param(name)``.
          - For unknown, it is the ``Parameter`` from ``inspect.signature``.
        """

        #Mostly to avoid all possible spurious attribute errors
        getprovider = self.get_provider_func
        func = getprovider(provider_name)
        sig = inspect.signature(func)
        result = [func]
        for param_name, param in sig.parameters.items():
            try:
                param_provider = getprovider(param_name)
            except AttributeError:
                config_func = self.input_parser.get_parse_func(param_name)
                if config_func:
                    result.append(('config', param_name,
                                   self.input_parser.explain_param(param_name)))
                else:
                    result.append(('unknown', param_name, param))
            else:
                result.append(('provider', param_name,
                               self.explain_provider(param_provider.__name__)))
        return result

    def resolve_targets(self):
        for target in self.targets:
            self.resolve_target(target)

    def resolve_target(self, target):
        name, fuzzy, extra_args = target
        specs = self.input_parser.process_fuzzyspec(fuzzy,
                                                self.rootns, parents=[name])
        for spec in specs:
            self.process_target(name, spec, extra_args)

    def process_target(self, name, nsspec, extraargs=None,
                            default=EMPTY):

        log.debug("Processing target %s" % name)

        gen = self._process_requirement(name, nsspec, extraargs=extraargs,
                                        default=default, parents=[])
        gen.send(None)
        try:
            gen.send(None)
        except StopIteration:
            pass
        else:
            raise RuntimeError()

    def _process_requirement(self, name, nsspec, *, extraargs=None,
                            default=EMPTY, parents=None):
        """Create nodes so as to satisfy the requirement specified by the
        arguments."""
        if parents is None:
            parents = []

        log.debug("Processing requirement: %s" % (name,))

        ns = namespaces.resolve(self.rootns, nsspec)
        if extraargs is None:
            extraargs = ()


        #First try to find the name in the namespace
        try:
            put_index, val = self.input_parser.resolve_key(name, ns, parents=parents)
            log.debug("Found %s for spec %s at %s"%(name, nsspec, put_index))

        except InputNotFoundError as e:
            #See https://www.python.org/dev/peps/pep-3110/
            saved_exception = e
            #Handle this case later
            pass
        else:
            if extraargs:
                raise ResourceNotUnderstood(name, "The resource %s name is "
                "already present in the input, but some arguments were "
                "passed to compute it: %s" % (name, extraargs), parents[-1])

            yield put_index, val
            return

        #If the name is not in the providers, either it is an extra argument
        #or is missing

        if not self.is_provider_func(name):
            if default is EMPTY:
                raise saved_exception
            else:
                put_index = None
                yield put_index, default
                return

        #here we handle the case where the requirement is a provider and
        #make a new node for it.
        yield from self._make_node(name, nsspec, extraargs, parents)


    def _make_node(self, name, nsspec, extraargs, parents):
        """Make a node from the input arguments as well as any nodes required
        from that node."""
        f = self.get_provider_func(name)
        if isinstance(f, collect):
            yield from self._make_collect(f, name, nsspec, parents)
        else:
            yield from self._make_callspec(f, name, nsspec, extraargs, parents)

    def _create_default_key(self, name, nsspec, put_index=None, defaults=None):
        """Push a namespace level for a node to store input values.
        Return the nsspec of that node."""
        if defaults is None:
            defaults = {}
        if put_index is None:
            put_index = 0

        defaults_label = '_' + name + '_defaults'
        nsspec = (*nsspec[:len(nsspec)-put_index], defaults_label)
        parent_ns = namespaces.resolve(self.rootns, nsspec[:-1])
        namespaces.push_nslevel(parent_ns, defaults_label, defaults)
        return nsspec


    def _make_callspec(self, f, name, nsspec, extraargs, parents):
        """Make a normal node that calls a function."""

        defaults = {}
        s = inspect.signature(f)
        if(extraargs):
            defaults.update(dict(extraargs))

        #Note that this is the latest possible put_index and not len - 1
        #because there is also the root namespace.
        put_index = len(nsspec)
        gens = []
        for param_name, param in s.parameters.items():
            default = defaults.get(param_name, param.default)
            gen = self._process_requirement(param_name, nsspec, extraargs=None,
                                     default=default, parents=[name, *parents])
            index, _ = gen.send(None)
            log.debug("put_index for %s is %s" % (param_name, index))
            if index is None:
                defaults[param_name] = default
            elif index < put_index:
                put_index = index
            gens.append(gen)

        #The namespace stack (put_index) goes in the opposite direction
        #of the nsspec. put_index==len(nsspec)==len(ns.maps)-1
        #corresponds to the root namespace, and put_index=0 to the current
        #spec.

        #We need the len bit for the case put_index==0
        newnsspec = self._create_default_key(name, nsspec, put_index, defaults)
        log.debug("New spec for %s is: %s" %(name, newnsspec,))


        ns = namespaces.resolve(self.rootns, newnsspec)


        cs = CallSpec(f, tuple(s.parameters.keys()), name,
                      newnsspec)
        log.debug("Appending node '%s'" % (cs,))
        self.graph.add_or_update_node(cs)
        for gen in gens:
            try:
               gen.send(cs)
            except StopIteration:
                pass
            else:
                raise RuntimeError()


        required_by = yield put_index, cs
        if required_by is None:
            outputs = set()
        else:
            outputs = set([required_by])
        self.graph.add_or_update_node(cs, outputs=outputs)

        if hasattr(f, 'checks'):
            for check in f.checks:
                try:
                    check(callspec=cs, ns=ns, graph=self.graph,
                          environment=self.environment)
                except CheckError as e:
                    raise ResourceError(name, e, parents)

    def _make_collect(self, f, name, nsspec, parents):
        """Make a node that spans a function over the values in a list and
        collects them in another list."""
        newparents = [name, *parents]

        myspec = self._create_default_key(name, nsspec)

        collspec = CollectSpec(f, (), name, myspec)

        required_by = yield 0, collspec

        if required_by is None:
            outputs = set()
        else:
            outputs = set([required_by])

        self.graph.add_or_update_node(collspec, outputs=outputs)

        total_fuzzyspec = nsspec + f.fuzzyspec
        specs = self.input_parser.process_fuzzyspec(total_fuzzyspec,
                                                    self.rootns,
                                                    newparents)
        f.result = OrderedDict.fromkeys(range(len(specs)))
        for i, spec in enumerate(specs):
            gen = self._make_node(f.function.__name__, spec, None,
                                            parents=newparents)

            index, newcs = gen.send(None)
            try:
                gen.send(collspec)
            except StopIteration:
                pass
            else:
                raise RuntimeError()

            flagargs = (('target', collspec), ('index', i))
            self._node_flags[newcs].add((add_to_dict_flag, flagargs))

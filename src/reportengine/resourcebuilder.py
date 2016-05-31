# -*- coding: utf-8 -*-
"""
Created on Fri Nov 13 21:18:06 2015

@author: zah
"""
from __future__ import generator_stop

from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor
import asyncio
import logging
import inspect
import enum
import functools

from reportengine import dag
from reportengine import namespaces
from reportengine.configparser import ConfigError
from reportengine.checks import CheckError
from reportengine.utils import comparepartial, ChainMap

log = logging.getLogger(__name__)

RESOURCE = "resource"
PROVIDER = "provider"

class provider:
    """Decorator intended to be used for the functions that are to
    be exposed as providers, either directly or trough more specialized
    decorators in reportengine."""
    def __init__(self, f):
        functools.update_wrapper(self, f)
        self.f = f

    def __call__(self, *args, **kwargs):
        return self.f(*args, **kwargs)



class ExecModes(enum.Enum):
    SET_UNIQUE = 'set_unique'
    SET_OR_UPDATE = "set_or_update"
    APPEND_UNORDERED = 'append_to'

CallSpec = namedtuple('CallSpec', ('function', 'kwargs', 'resultname',
                                  'execmode','nsspec'))
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

    if spec.execmode in (ExecModes.SET_OR_UPDATE, ExecModes.SET_UNIQUE):
        return "{res} = {f}({callargs})".format(res=res,
                                        f=f,
                                        callargs=callargs)
    elif spec.execmode == ExecModes.APPEND_UNORDERED:
        return "{res}.append({f}({callargs}))".format(res=res,
                                        f=f,
                                        callargs=callargs)

CallSpec.__str__ = print_callspec

class ResourceExecutor():

    def __init__(self, graph, rootns, environment=None):
        self.graph = graph
        self.rootns = rootns
        self.environment = environment

    def resolve_kwargs(self, nsspec, kwargs):
        namespace = namespaces.resolve(self.rootns, nsspec)
        kwdict = {}
        put_index = len(namespace.maps) - 1
        for kw in kwargs:
            index, kwdict[kw] =  namespace.get_where(kw)
            #We ignore the indeternal default namespace for the function
            if index > 0 and index < put_index:
                put_index = index
        kwdict = {kw: namespace[kw] for kw in kwargs}

        #TODO: Remove the put_index logic from here. It is already handled
        #in _process_requirement.
        assert(put_index==1)
        return kwdict, put_index



    def execute_sequential(self):
        for node in self.graph:
            function, kwargs, resultname, mode, nsspec = spec = node.value
            kwdict, put_index = self.resolve_kwargs(nsspec, kwargs)
            result = self.get_result(function, **kwdict)
            self.set_result(result, spec, put_index)

    #This needs to be a staticmethod, because otherwise we have to serialize
    #the whole self object when passing to multiprocessing.
    @staticmethod
    def get_result(function, **kwdict):
        return function(**kwdict)


    def set_result(self, result, spec, put_index):
        function, kwargs, resultname, execmode, nsspec = spec
        namespace = namespaces.resolve(self.rootns, nsspec)
        put_map = namespace.maps[put_index]
        log.debug("put index: %s, spec: %s" % (put_index, nsspec))
        if not execmode in ExecModes:
            raise TypeError("Callspecmode must be an ExecMode")

        if execmode == ExecModes.SET_UNIQUE:
            if resultname in put_map:
                raise ValueError("Resource already set: %s" % resultname)
            put_map[resultname] = result

        elif execmode == ExecModes.SET_OR_UPDATE:
            put_map[resultname] = result

        elif execmode == ExecModes.APPEND_UNORDERED:
            if not resultname in namespace:
                put_map[resultname] = []
            put_map[resultname].append(result)

        else:
            raise NotImplementedError(execmode)

        if hasattr(function, 'final_action'):
            function.final_action(result,
                                  self.environment,
                                  spec, self.rootns, self.graph)

    async def submit_next_specs(self, loop, executor, next_specs, deps):
        tasks = []
        for spec in next_specs:
            kwdict, put_index = self.resolve_kwargs(spec.nsspec, spec.kwargs)
            clause = comparepartial(self.get_result, spec.function, **kwdict)
            future = loop.run_in_executor(executor, clause)


            spec_done = self._spec_done(future=future,
                               loop=loop, executor=executor,
                               spec=spec, deps=deps, put_index=put_index)

            task = loop.create_task(spec_done)
            tasks.append(task)
        await asyncio.gather(*tasks)

    async def _spec_done(self, future, loop, executor, spec, deps, put_index):
        result = await future
        self.set_result(result, spec, put_index)
        try:
            next_specs = deps.send(spec)
        except StopIteration:
            pass
        else:
             await self.submit_next_specs(loop, executor, next_specs, deps)

    def execute_parallel(self, executor=None, loop=None):

        if executor is None:
            executor = ProcessPoolExecutor()
            shut_executor = True
        else:
            shut_executor = False

        if loop is None:
            loop = asyncio.get_event_loop()

        deps = self.graph.dependency_resolver()
        next_specs = deps.send(None)


        task = loop.create_task(self.submit_next_specs(loop, executor,
                                                    next_specs, deps))
        loop.run_until_complete(task)

        if shut_executor:
            executor.shutdown()

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
        return "Could not process the resource %s, required by:\n%s\n%s"%(
                self.name, '\n'.join(' - ' + p for p in self.parents),
                self.message)

class ResourceNotUnderstood(ResourceError, TypeError): pass

Target = namedtuple('Target', ('name', 'nsspec', 'extraargs'))

EMPTY = inspect.Signature.empty

class ResourceBuilder(ResourceExecutor):

    def __init__(self, input_parser, providers, targets, environment=None):

        self.input_parser = input_parser
        self.providers = providers
        self.targets = targets

        self.rootns = ChainMap()
        self.graph = dag.DAG()

        self.environment = environment

    def resolve_targets(self):
        for target in self.targets:
            self.resolve_target(target)

    def resolve_target(self, target):
        name, fuzzy, extra_args = target
        try:
            specs = self.input_parser.process_fuzzyspec(fuzzy,
                                                self.rootns, parents=[name])
        except ConfigError as e:
            raise
        except Exception as e:
            raise ResourceError(target, e, None)
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
        if parents is None:
            parents = []

        log.debug("Processing requirement: %s" % (name,))

        ns = namespaces.resolve(self.rootns, nsspec)
        if extraargs is None:
            extraargs = ()


        #First try to find the name in the namespace
        try:
            put_index, _ = self.input_parser.resolve_key(name, ns, parents=parents)
            log.debug("Found %s for spec %s at %s"%(name, nsspec, put_index))

        except KeyError as e:
            #See https://www.python.org/dev/peps/pep-3110/
            saved_exception = e
            #Handle this case later
            pass
        else:
            if extraargs:
                raise ResourceNotUnderstood(name, "The resource %s name is "
                "already present in the input, but some arguments were "
                "passed to compute it: %s" % (name, extraargs), parents[-1])

            yield put_index
            return

        #If the name is not in the providers, either it is an extra argument
        #or is missing

        if not hasattr(self.providers, name):
            if default is EMPTY:
                raise saved_exception
            else:
                put_index = None
                yield put_index
                return

        #here we handle the case where the requirement is a provider and
        #make a new node for it.
        yield from self._make_node(name, nsspec, extraargs, parents)

    def _make_node(self, name, nsspec, extraargs, parents):

        defaults_label = '_' + name + '_defaults'
        defaults = {}

        f = getattr(self.providers, name)
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
            index = gen.send(None)
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
        nsspec = (*nsspec[:len(nsspec)-put_index], defaults_label)
        log.debug("New spec for %s is: %s" %(name, nsspec,))

        parent_ns = namespaces.resolve(self.rootns, nsspec[:-1])
        namespaces.push_nslevel(parent_ns, defaults_label, defaults)
        ns = namespaces.resolve(self.rootns, nsspec)


        cs = CallSpec(f, tuple(s.parameters.keys()), name,
                      ExecModes.SET_UNIQUE,
                      nsspec)
        log.debug("Appending node '%s'" % (cs,))
        self.graph.add_or_update_node(cs)
        for gen in gens:
            try:
               gen.send(cs)
            except StopIteration:
                pass
            else:
                raise RuntimeError()


        required_by = yield put_index
        if required_by is None:
            outputs = set()
        else:
            outputs = set([required_by])
        self.graph.add_or_update_node(cs, outputs=outputs)

        if hasattr(f, 'checks'):
            for check in f.checks:
                try:
                    check(cs, ns, self.graph)
                except CheckError as e:
                    raise ResourceError(name, e, [req.resultname
                                               for req in outputs])

# -*- coding: utf-8 -*-
'''
Manage the process of the overstate. The overstate is a means to orchestrate
the deployment of states over groups of servers.
'''

# 1. Read in overstate
# 2. Create initial order
# 3. Start list evaluation
# 4. Verify requisites
# 5. Execute state call
# 6. append data to running

# Import python libs
import os

# Import salt libs
import salt.client
import salt.utils

# Import third party libs
import yaml


class OverState(object):
    '''
    Manage sls file calls over multiple systems
    '''
    def __init__(self, opts, env='base', overstate=None):
        self.opts = opts
        self.env = env
        self.over = self.__read_over(overstate)
        self.names = self._names()
        self.local = salt.client.LocalClient(self.opts['conf_file'])
        self.over_run = {}

    def __read_over(self, overstate):
        '''
        Read in the overstate file
        '''
        if overstate:
            with salt.utils.fopen(overstate) as fp_:
                try:
                    # TODO Use render system
                    return self.__sort_stages(yaml.safe_load(fp_))
                except Exception:
                    return {}
        if self.env not in self.opts['file_roots']:
            return {}
        for root in self.opts['file_roots'][self.env]:
            fn_ = os.path.join(
                    root,
                    self.opts.get('overstate', 'overstate.sls')
                    )
            if not os.path.isfile(fn_):
                continue
            with salt.utils.fopen(fn_) as fp_:
                try:
                    # TODO Use render system
                    return self.__sort_stages(yaml.safe_load(fp_))
                except Exception:
                    return {}
        return {}

    def __sort_stages(self, pre_over):
        '''
        Generate the list of executions
        '''
        comps = []
        for key in sorted(pre_over):
            comps.append({key: pre_over[key]})
        return comps

    def _stage_list(self, match):
        '''
        Return a list of ids cleared for a given stage
        '''
        if isinstance(match, list):
            match = ' or '.join(match)
        raw = self.local.cmd(match, 'test.ping', expr_form='compound')
        return raw.keys()

    def _names(self):
        '''
        Return a list of names defined in the overstate
        '''
        names = set()
        for comp in self.over:
            names.add(comp.keys()[0])
        return names

    def get_stage(self, name):
        '''
        Return the named stage
        '''
        for stage in self.over:
            if name in stage:
                return stage

    def verify_stage(self, stage):
        '''
        Verify that the stage is valid, return the stage, or a list of errors
        '''
        errors = []
        if 'match' not in stage:
            errors.append('No "match" argument in stage.')
        if errors:
            return errors
        return stage

    def call_stage(self, name, stage):
        '''
        Check if a stage has any requisites and run them first
        '''
        fun = 'state.highstate'
        arg = ()
        req_fail = {name: {}}
        if 'sls' in stage:
            fun = 'state.sls'
            arg = (','.join(stage['sls']), self.env)
        elif 'function' in stage or 'fun' in stage:
            fun_d = stage.get('function', stage.get('fun'))
            if not fun_d:
                # Function dict is empty
                yield {name: {}}
            if isinstance(fun_d, str):
                fun = fun_d
            elif isinstance(fun_d, dict):
                fun = fun_d.keys()[0]
                arg = fun_d[fun]
            else:
                yield {name: {}}
        if 'require' in stage:
            for req in stage['require']:
                if req in self.over_run:
                    # The req has been called, check it
                    for minion in self.over_run[req]:
                        running = {minion: self.over_run[req][minion]['ret']}
                        if self.over_run[req][minion]['fun'] == 'state.highstate':
                            if salt.utils.check_state_result(running):
                                # This req is good, check the next
                                continue
                        elif self.over_run[req][minion]['fun'] == 'state.sls':
                            if salt.utils.check_state_result(running):
                                # This req is good, check the next
                                continue
                        else:
                            if not self.over_run[req][minion]['retcode']:
                                if self.over_run[req][minion]['success']:
                                    continue
                        tag_name = 'req_|-fail_|-fail_|-None'
                        failure = {tag_name: {
                            'ret': {
                                    'result': False,
                                    'comment': 'Requisite {0} failed for stage on minion {1}'.format(req, minion),
                                    'name': 'Requisite Failure',
                                    'changes': {},
                                    '__run_num__': 0,
                                        },
                            'retcode': 254,
                            'success': False,
                            'fun': 'req.fail',
                            }
                            }
                        self.over_run[name] = failure
                        req_fail[name].update(failure)
                elif req not in self.names:
                    tag_name = 'No_|-Req_|-fail_|-None'
                    failure = {tag_name: {
                        'ret': {
                            'result': False,
                            'comment': 'Requisite {0} not found'.format(req),
                            'name': 'Requisite Failure',
                            'changes': {},
                            '__run_num__': 0,
                                },
                            'retcode': 253,
                            'success': False,
                            'fun': 'req.fail',
                            }
                            }
                    self.over_run[name] = failure
                    req_fail[name].update(failure)
                else:
                    for comp in self.over:
                        rname = comp.keys()[0]
                        if req == rname:
                            rstage = comp[rname]
                            v_stage = self.verify_stage(rstage)
                            if isinstance(v_stage, list):
                                yield {rname: v_stage}
                            else:
                                yield self.call_stage(rname, rstage)
        if req_fail[name]:
            yield req_fail
        else:
            ret = {}
            tgt = self._stage_list(stage['match'])
            cmd_kwargs = {
                'tgt': tgt,
                'fun': fun,
                'arg': arg,
                'expr_form': 'list',
                'raw': True}
            if 'batch' in stage:
                local_cmd = self.local.cmd_batch
                cmd_kwargs['batch'] = stage['batch']
            else:
                local_cmd = self.local.cmd_iter
            for minion in local_cmd(**cmd_kwargs):
                if all(key not in minion for key in ('id', 'return', 'fun')):
                    continue
                ret.update({minion['id']:
                        {
                        'ret': minion['return'],
                        'fun': minion['fun'],
                        'retcode': minion.get('retcode', 0),
                        'success': minion.get('success', True),
                        }
                    })
            self.over_run[name] = ret
            yield {name: ret}

    def stages(self):
        '''
        Execute the stages
        '''
        self.over_run = {}
        for comp in self.over:
            name = comp.keys()[0]
            stage = comp[name]
            if name not in self.over_run:
                self.call_stage(name, stage)

    def stages_iter(self):
        '''
        Return an iterator that yields the state call data as it is processed
        '''
        def yielder(gen_ret):
            if (not isinstance(gen_ret, list)
                    and not isinstance(gen_ret, dict)
                    and hasattr(gen_ret, 'next')):
                for sub_ret in gen_ret:
                    for yret in yielder(sub_ret):
                        yield yret
            else:
                yield gen_ret

        self.over_run = {}
        yield self.over
        for comp in self.over:
            name = comp.keys()[0]
            stage = comp[name]
            if name not in self.over_run:
                v_stage = self.verify_stage(stage)
                if isinstance(v_stage, list):
                    yield [comp]
                    yield v_stage
                else:
                    for sret in self.call_stage(name, stage):
                        for yret in yielder(sret):
                            sname = yret.keys()[0]
                            yield [self.get_stage(sname)]
                            final = {}
                            for minion in yret[sname]:
                                final[minion] = yret[sname][minion]['ret']
                            yield final

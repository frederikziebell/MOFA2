
"""
This module is used to define the class containing the entire Bayesian Network,
and the corresponding attributes/methods to train the model, set algorithmic options, calculate lower bound, etc.
"""

from __future__ import division
from time import time
import os
import scipy as s
import pandas as pd
import sys
import numpy.ma as ma
import math
import resource

from mofapy2.core.nodes.variational_nodes import Variational_Node
from mofapy2.core import gpu_utils
from .utils import corr, nans, infer_platform

import warnings
warnings.filterwarnings("ignore")

class BayesNet(object):
    def __init__(self, dim, nodes):
        """ Initialisation of a Bayesian network

        PARAMETERS
        ----------
        dim: dict
            keyworded dimensionalities, ex. {'N'=10, 'M'=3, ...}
        nodes: dict
            dictionary with all nodes where the keys are the name of the node and the values are instances the 'Node' class
        """

        self.dim = dim
        self.nodes = nodes
        self.options = None  # TODO rename to train_options everywhere

        # Training and simulations flag
        self.trained = False
        self.simulated = False

        # Set GPU mode
        # gpu_utils.gpu_mode = options['gpu_mode']

    def setTrainOptions(self, train_opts):
        """ Method to store training options """

        # Sanity checks
        assert "maxiter" in train_opts, "'maxiter' not found in the training options dictionary"
        assert "start_drop" in train_opts, "'start_drop' not found in the training options dictionary"
        assert "freq_drop" in train_opts, "'freq_drop' not found in the training options dictionary"
        assert "verbose" in train_opts, "'verbose' not found in the training options dictionary"
        assert "quiet" in train_opts, "'quiet' not found in the training options dictionary"
        assert "tolerance" in train_opts, "'tolerance' not found in the training options dictionary"
        assert "convergence_mode" in train_opts, "'convergence_mode' not found in the training options dictionary"
        assert "forceiter" in train_opts, "'forceiter' not found in the training options dictionary"
        assert "schedule" in train_opts, "'schedule' not found in the training options dictionary"
        assert "start_sparsity" in train_opts, "'start_sparsity' not found in the training options dictionary"
        assert "gpu_mode" in train_opts, "'gpu_mode' not found in the training options dictionary"
        assert "start_elbo" in train_opts, "'gpu_mode' not found in the training options dictionary"

        self.options = train_opts

    def getParameters(self, *nodes):
        """ Method to collect all parameters of a given set of nodes

        PARAMETERS
        ----------
        nodes: iterable
            name of the nodes (all nodes by default)
        """

        if len(nodes) == 0: nodes = self.nodes.keys()
        params = {}
        for node in nodes:
            tmp = self.nodes[node].getParameters()
            if tmp != None: params[node] = tmp
        return params

    def getExpectations(self, only_first_moments=False, *nodes):
        """Method to collect all expectations of a given set of nodes

        PARAMETERS
        ----------
        only_first_moments: bool
            get only first moments? (Default is False)
        nodes: list
            name of the nodes (Default is all nodes)
        """

        if len(nodes) == 0: nodes = self.nodes.keys()
        expectations = {}
        for node in nodes:
            if only_first_moments:
                tmp = self.nodes[node].getExpectation()
            else:
                tmp = self.nodes[node].getExpectations()
            expectations[node] = tmp
        return expectations

    def getNodes(self):
        """ Method to return all nodes """
        return self.nodes

    def calculate_variance_explained(self):

        # Collect relevant expectations
        Z = self.nodes['Z'].getExpectation()
        W = self.nodes["W"].getExpectation()
        Y = self.nodes["Y"].getExpectation()

        # Get groups
        groups = self.nodes["AlphaZ"].groups if "AlphaZ" in self.nodes else s.array([0]*self.dim['N'])

        r2 = [ s.zeros([self.dim['M'], self.dim['K']])] * self.dim['G']
        for m in range(self.dim['M']):
            mask = self.nodes["Y"].getNodes()[m].getMask()
            for g in range(self.dim['G']):
                gg = groups==g
                SS = s.square(Y[m][gg,:]).sum()
                for k in range(self.dim['K']):
                    Ypred_mk = s.outer(Z[gg,k], W[m][:,k])
                    Ypred_mk[mask[gg,:]] = 0.
                    Res_k = ((Y[m][gg,:] - Ypred_mk)**2.).sum()
                    r2[g][m,k] = 1. - Res_k/SS
        return r2

    def calculate_total_variance_explained(self):

        # Collect relevant expectations
        Z = self.nodes['Z'].getExpectation()
        W = self.nodes["W"].getExpectation()
        Y = self.nodes["Y"].getExpectation()

        r2 = s.zeros(self.dim['M'])
        for m in range(self.dim['M']):
            mask = self.nodes["Y"].getNodes()[m].mask

            Ypred_m = s.dot(Z, W[m].T)
            Ypred_m[mask] = 0.

            Res = ((Y[m].data - Ypred_m)**2.).sum()
            SS = s.square(Y[m]).sum()

            r2[m] = 1. - Res/SS

        return r2

    def removeInactiveFactors(self, min_r2=None):
        """Method to remove inactive factors

        PARAMETERS
        ----------
        min_r2: float
            threshold to shut down factors based on a minimum variance explained per group and view
        """
        drop_dic = {}

        if min_r2 is not None:
            r2 = self.calculate_variance_explained()

            tmp = [ s.where( (r2[g]>min_r2).sum(axis=0) == 0)[0] for g in range(self.dim['G']) ]
            drop_dic["min_r2"] = list(set.intersection(*map(set,tmp)))
            if len(drop_dic["min_r2"]) > 0:
                drop_dic["min_r2"] = [ s.random.choice(drop_dic["min_r2"]) ]

        # Drop the factors
        drop = s.unique(s.concatenate(list(drop_dic.values())))
        if len(drop) > 0:
            for node in self.nodes.keys():
                self.nodes[node].removeFactors(drop)
        self.dim['K'] -= len(drop)

        if self.dim['K']==0:
            print("All factors shut down, no structure found in the data.")
            exit()

        pass

    def precompute(self):
        # Precompute terms
        for n in self.nodes:
            self.nodes[n].precompute(self.options)

        # Precompute ELBO
        for node in self.nodes["Y"].getNodes(): node.TauTrick = False # important to do this for ELBO computation
        elbo = self.calculateELBO()
        for node in self.nodes["Y"].getNodes(): node.TauTrick = self.options["Y_ELBO_TauTrick"]

        if self.options['verbose']:
            print("ELBO before training:")
            print("".join([ "%s=%.2f  " % (k,v) for k,v in elbo.drop("total").iteritems() ]) + "\nTotal: %.2f\n" % elbo["total"])
        else:
            if not self.options['quiet']:
                print('ELBO before training: %.2f' % elbo["total"])
        print("\n")

        return elbo

    def iterate(self):
        """Method to start iterating and updating the variables using the VB algorithm"""

        # Define some variables to monitor training
        nodes = list(self.getVariationalNodes().keys())
        elbo = pd.DataFrame(data = nans((self.options['maxiter']+1, len(nodes)+1 )), columns = nodes+["total"] )
        number_factors = nans((self.options['maxiter']+1))
        iter_time = nans((self.options['maxiter']+1))

        # Precompute
        converged = False; convergence_token = 1
        elbo.iloc[0] = self.precompute()
        number_factors[0] = self.dim['K']
        iter_time[0] = 0.

        for i in range(1,self.options['maxiter']):
            t = time();

            # Remove inactive factors
            if (i>=self.options["start_drop"]) and (i%self.options['freq_drop']) == 0:
                if self.options['drop']["min_r2"] is not None:
                    self.removeInactiveFactors(**self.options['drop'])
                number_factors[i] = self.dim["K"]

            # Update node by node, with E and M step merged
            t_updates = time()
            for node in self.options['schedule']:
                if (node=="ThetaW" or node=="ThetaZ") and i<self.options['start_sparsity']:
                    continue
                self.nodes[node].update()
            t_updates = time() - t_updates

            # Calculate Evidence Lower Bound
            if (i>=self.options["start_elbo"]) and ((i-self.options["start_elbo"])%self.options['elbofreq']==0):
                t_elbo = time()
                elbo.iloc[i] = self.calculateELBO()
                t_elbo = time() - t_elbo

                # Check convergence using the ELBO
                if i==self.options["start_elbo"]: 
                    delta_elbo = elbo.iloc[i]["total"]-elbo.iloc[0]["total"]
                else:
                    delta_elbo = elbo.iloc[i]["total"]-elbo.iloc[i-self.options['elbofreq']]["total"]

                # Print ELBO monitoring
                if not self.options['quiet']:
                    print("Iteration %d: time=%.2f, ELBO=%.2f, deltaELBO=%.3f (%.9f%%), Factors=%d" % (i, time()-t, elbo.iloc[i]["total"], delta_elbo, 100*abs(delta_elbo/elbo.iloc[0]["total"]), (self.dim['K'])))
                    if delta_elbo<0 and not self.options['stochastic']: print("Warning, lower bound is decreasing...\a")

                # Print ELBO decomposed by node and variance explained
                if self.options['verbose']:
                    print("".join([ "%s=%.2f  " % (k,v) for k,v in elbo.iloc[i].drop("total").iteritems() ]))
                    print('Time spent in ELBO computation: %.1f%%' % (100*t_elbo/(t_updates+t_elbo)) )

                # Assess convergence
                if i>self.options["start_elbo"] and not self.options['forceiter']:
                    convergence_token, converged = self.assess_convergence(delta_elbo, elbo.iloc[0]["total"], convergence_token)
                    if converged:
                        number_factors = number_factors[:i]; elbo = elbo[:i]
                        print ("\nConverged!\n"); break

            # Do not calculate lower bound
            else:
                if not self.options['quiet']: print("Iteration %d: time=%.2f, Factors=%d" % (i,time()-t,self.dim["K"]))

            # Print other statistics
            if self.options['verbose']:
                # Memory usage
                # print('Peak memory usage: %.2f MB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / infer_platform() ))
                # Variance explained
                r2 = self.calculate_total_variance_explained()
                print("Variance explained:\t" + "   ".join([ "View %s: %.3f%%" % (m,100*r2[m]) for m in range(self.dim["M"])]))
                # Sparsity levels of the weights
                # W = self.nodes["W"].getExpectation()
                # foo = [s.mean(s.absolute(W[m])<1e-3) for m in range(self.dim["M"])]
                # print("Fraction of zero weights:\t" + "   ".join([ "View %s: %.0f%%" % (m,100*foo[m]) for m in range(self.dim["M"])]))
                # Sparsity levels of the factors
                # Z = self.nodes["Z"].getExpectation()
                # bar = s.mean(s.absolute(Z)<1e-3)
                # print("Fraction of zero samples: %.0f%%" % (100*bar))
                print("\n")

            iter_time[i] = time()-t
            
            # Flush (we need this to print when running on the cluster)
            sys.stdout.flush()

        # Finish by collecting the training statistics
        self.train_stats = { 'time':iter_time, 'number_factors':number_factors, 'elbo':elbo["total"].values, 'elbo_terms':elbo.drop("total",1) }
        self.trained = True

    def assess_convergence(self, delta_elbo, first_elbo, convergence_token):
        converged = False

        # Option 1: deltaELBO
        # if abs(delta_elbo) < self.options['tolerance']: 
        #     converged = True

        # Assess convergence based on the fraction of deltaELBO change
        if self.options["convergence_mode"] == "fast":
            convergence_threshold = 0.00001
        elif self.options["convergence_mode"] == "medium":
            convergence_threshold = 0.000001
        elif self.options["convergence_mode"] == "slow":
            convergence_threshold = 0.0000001
        else:
            print("Convergence mode not recognised"); exit()

        if 100*abs(delta_elbo/first_elbo) < convergence_threshold: 
            convergence_token += 1
            if convergence_token==5: converged = True
        else:
            convergence_token = 1

        return convergence_token, converged

    def getVariationalNodes(self):
        """ Method to return all variational nodes """
        # TODO problem with dictionnary comprehension here
        to_ret = {}
        for node in self.nodes.keys():
            if isinstance(self.nodes[node],Variational_Node):
                to_ret[node] =self.nodes[node]

        return to_ret
        # return { node:self.nodes[node] for node in self.nodes.keys() if isinstance(self.nodes[node],Variational_Node)}
        # return { k:v for k,v in self.nodes.items() if isinstance(v,Variational_Node) }

    def getTrainingStats(self):
        """ Method to return training statistics """
        return self.train_stats

    def getTrainingOpts(self):
        """ Method to return training options """
        return self.options

    def getTrainingData(self):
        """ Method to return training data """
        return self.nodes["Y"].getValues()

    def calculateELBO(self, *nodes):
        """Method to calculate the Evidence Lower Bound of the model"""
        if len(nodes) == 0: nodes = self.getVariationalNodes().keys()
        elbo = pd.Series(s.zeros(len(nodes)+1), index=list(nodes)+["total"])
        for node in nodes:
            elbo[node] = float(self.nodes[node].calculateELBO())
            elbo["total"] += elbo[node]
        return elbo


class StochasticBayesNet(BayesNet):
    def __init__(self, dim, nodes):
        super().__init__(dim=dim, nodes=nodes)

    def step_size(self, i):
        # return the step size for the considered iteration
        return (i + self.options['learning_rate'])**(-self.options['forgetting_rate'])

    def step_size2(self, i):
        # return the step size for the considered iteration
        return self.options['learning_rate'] / ((1 + self.options['forgetting_rate'] * i)**(3./4.))

    def sample_mini_batch(self):
        # TODO if multiple group, sample indices in each group evenly ? prob yes
        S = int( self.options['batch_size'] * self.dim['N'] )
        ix = s.random.choice(range(self.dim['N']), size=S, replace=False)
        self.define_mini_batch(ix)
        return ix

    def sample_mini_batch_no_replace(self, i):
        """ Method to define mini batches"""

        # TODO :
        # - if multiple group, sample indices in each group evenly ? prob yes

        i -= 1 # This is because we start at iteration 1 in the main loop

        # Sample mini-batch indices and define epoch
        n_batches = math.ceil(1./self.options['batch_size'])
        S = self.options['batch_size'] * self.dim['N']
        batch_ix = i % n_batches
        epoch = int(i / n_batches)
        if batch_ix == 0:
            print("## Epoch %s ##" % str(epoch+1))
            print("-------------------------------------------------------------------------------------------")
            self.shuffled_ix = s.random.choice(range(self.dim['N']), size= self.dim['N'], replace=False)

        min = int(S * batch_ix)
        max = int(S * (batch_ix + 1))
        if max > self.dim['N']:
            max = self.dim['N']

        ix = self.shuffled_ix[min:max]
        self.define_mini_batch(ix)

        return ix, epoch
    
    def define_mini_batch(self, ix):
        # Define mini-batch for each node
        self.nodes['Y'].define_mini_batch(ix)
        self.nodes['Tau'].define_mini_batch(ix)
        if 'AlphaZ' in self.nodes:
            self.nodes['AlphaZ'].define_mini_batch(ix)
        if 'ThetaZ' in self.nodes:
            self.nodes['ThetaZ'].define_mini_batch(ix)  

    def iterate(self):
        """Method to start iterating and updating the variables using the VB algorithm"""

        # Define some variables to monitor training
        nodes = list(self.getVariationalNodes().keys())
        elbo = pd.DataFrame(data = nans((self.options['maxiter']+1, len(nodes)+1 )), columns = nodes+["total"] )
        number_factors = nans((self.options['maxiter']+1))
        iter_time = nans((self.options['maxiter']+1))

        # Precompute
        converged = False; convergence_token = 1
        elbo.iloc[0] = self.precompute()
        number_factors[0] = self.dim['K']
        iter_time[0] = 0.

        # Print stochastic settings before training
        print("Using stochastic variational inference with the following parameters:")
        print("- Batch size (fraction of samples): %.2f\n- Forgetting rate: %.2f\n- Learning rate: %.2f\n- Starts at iteration: %d \n" % 
            (100*self.options['batch_size'], self.options['forgetting_rate'], self.options['learning_rate'], self.options['start_stochastic']) )
        ix = None

        for i in range(1,self.options['maxiter']):
            t = time();

            # Sample mini-batch and define step size for stochastic inference
            if i>=(self.options["start_stochastic"]):
                ix, epoch = self.sample_mini_batch_no_replace(i-(self.options["start_stochastic"]-1))
                ro = self.step_size2(epoch)
            else:
                ro = 1.

            # Remove inactive factors
            if (i>=self.options["start_drop"]) and (i%self.options['freq_drop']) == 0:
                # if any(self.options['drop'].values()):
                if self.options['drop']["min_r2"] is not None:
                    self.removeInactiveFactors(**self.options['drop'])
                number_factors[i] = self.dim["K"]

            # Update node by node, with E and M step merged
            t_updates = time()
            for node in self.options['schedule']:
                if (node=="ThetaW" or node=="ThetaZ") and i<self.options['start_sparsity']:
                    continue
                self.nodes[node].update(ix, ro)
            t_updates = time() - t_updates

            # Calculate Evidence Lower Bound
            if (i>=self.options["start_elbo"]) and ((i-self.options["start_elbo"])%self.options['elbofreq']==0):
                t_elbo = time()
                elbo.iloc[i] = self.calculateELBO()
                t_elbo = time() - t_elbo

                # Check convergence using the ELBO
                if i==self.options["start_elbo"]: 
                    delta_elbo = elbo.iloc[i]["total"]-elbo.iloc[0]["total"]
                else:
                    delta_elbo = elbo.iloc[i]["total"]-elbo.iloc[i-self.options['elbofreq']]["total"]

                # Print ELBO monitoring
                print("Iteration %d: time=%.2f, ELBO=%.2f, deltaELBO=%.3f (%.9f%%), Factors=%d" % (i, time()-t, elbo.iloc[i]["total"], delta_elbo, 100*abs(delta_elbo/elbo.iloc[0]["total"]), (self.dim['K'])))
                if delta_elbo<0 and not self.options['stochastic']: print("Warning, lower bound is decreasing...\a")

                # Print ELBO decomposed by node and variance explained
                if self.options['verbose']:
                    print("".join([ "%s=%.2f  " % (k,v) for k,v in elbo.iloc[i].drop("total").iteritems() ]))
                    print('Time spent in ELBO computation: %.1f%%' % (100*t_elbo/(t_updates+t_elbo)) )

                # Assess convergence
                if i>self.options["start_elbo"] and not self.options['forceiter']:
                    convergence_token, converged = self.assess_convergence(delta_elbo, elbo.iloc[0]["total"], convergence_token)
                    if converged:
                        number_factors = number_factors[:i]
                        elbo = elbo[:i]
                        iter_time = iter_time[:i]
                        print ("\nConverged!\n"); break

            # Do not calculate lower bound
            else:
                print("Iteration %d: time=%.2f, Factors=%d" % (i,time()-t,self.dim["K"]))

            # Print other statistics
            print("Step size (rho): %.3f" % ro )
            if self.options['verbose']:
                # Memory usage
                # print('Peak memory usage: %.2f MB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / infer_platform() ))
                # Variance explained
                r2 = self.calculate_total_variance_explained()
                print("Variance explained:\t" + "   ".join([ "View %s: %.3f%%" % (m,100*r2[m]) for m in range(self.dim["M"])]))
                # Sparsity levels of the weights
                # W = self.nodes["W"].getExpectation()
                # foo = [s.mean(s.absolute(W[m])<1e-3) for m in range(self.dim["M"])]
                # print("Fraction of zero weights:\t" + "   ".join([ "View %s: %.0f%%" % (m,100*foo[m]) for m in range(self.dim["M"])]))
                # Sparsity levels of the factors
                # Z = self.nodes["Z"].getExpectation()
                # bar = s.mean(s.absolute(Z)<1e-3)
                # print("Fraction of zero samples: %.0f%%" % (100*bar))
            print("")

            iter_time[i] = time()-t
            
            # Flush (we need this to print when running on the cluster)
            sys.stdout.flush()

        # Finish by collecting the training statistics
        self.train_stats = { 'time':iter_time, 'number_factors':number_factors, 'elbo':elbo["total"].values, 'elbo_terms':elbo.drop("total",1) }
        self.trained = True
from __future__ import print_function

import numpy
import argparse
import logging
import pprint

import theano
from theano import tensor

from groundhog.mainLoop import MainLoop
from groundhog.datasets import LMIterator
from groundhog.trainer.SGD import SGD

from blocks.bricks import Application, Tanh
from blocks.bricks.recurrent import GatedRecurrent
from blocks.select import Selector
from blocks.graph import ComputationGraph, Cost
from blocks.bricks.sequence_generators import (
    SequenceGenerator, LinearReadout, SoftmaxEmitter, LookupFeedback)
from blocks.initialization import Orthogonal, IsotropicGaussian, Constant
from blocks.groundhog import GroundhogState, GroundhogModel
from blocks.serialization import load_params
from blocks.utils import shared_floatx_zeros

floatX = theano.config.floatX

logger = logging.getLogger()


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s: %(name)s: %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        "Case study of language modeling with RNN",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "mode", choices=["train", "sample"],
        help="The mode to run. Use `train` to train a new model"
             " and `sample` to sample a sequence generated by an"
             " existing one.")
    parser.add_argument(
        "prefix", default="sine",
        help="The prefix for model, timing and state files")
    parser.add_argument(
        "state", nargs="?", default="",
        help="Changes to Groundhog state")
    parser.add_argument("--path", help="Path to a language dataset")
    parser.add_argument("--dict", help="Path to the dataset dictionary")
    parser.add_argument("--restart", help="Start anew")
    parser.add_argument(
        "--reset", action="store_true", default=False,
        help="Reset the hidden state between batches")
    parser.add_argument(
        "--steps", type=int, default=100,
        help="Number of steps to plot for the 'sample' mode"
             " OR training sequence length for the 'train' mode.")
    args = parser.parse_args()
    logger.debug("Args:\n" + str(args))

    dim = 200
    num_chars = 50

    transition = GatedRecurrent(
        name="transition", activation=Tanh(), dim=dim,
        weights_init=Orthogonal())
    generator = SequenceGenerator(
        LinearReadout(readout_dim=num_chars, source_names=["states"],
                      emitter=SoftmaxEmitter(name="emitter"),
                      feedbacker=LookupFeedback(
                          num_chars, dim, name='feedback'),
                      name="readout"),
        transition,
        weights_init=IsotropicGaussian(0.01), biases_init=Constant(0),
        name="generator")
    generator.allocate()
    logger.debug("Parameters:\n" +
                 pprint.pformat(
                     [(key, value.get_value().shape) for key, value
                      in Selector(generator).get_params().items()],
                     width=120))

    if args.mode == "train":
        batch_size = 1
        seq_len = args.steps

        generator.initialize()

        # Build cost computation graph that uses the saved hidden states.
        # An issue: for Groundhog this is completely transparent, that's
        # why it does not carry the hidden state over the period when
        # validation in done. We should find a way to fix in the future.
        x = tensor.lmatrix('x')
        init_states = shared_floatx_zeros((batch_size, dim),
                                          name='init_states')
        reset = tensor.scalar('reset')
        cost = Cost(generator.cost(x, states=init_states * reset).sum())
        # TODO: better search routine
        states = [v for v in cost.variables
                  if hasattr(v.tag, 'application_call')
                  and v.tag.application_call.brick == generator.transition
                  and (v.tag.application_call.application ==
                       generator.transition.apply)
                  and v.tag.role == Application.OUTPUT_VARIABLE
                  and v.tag.name == 'states']
        assert len(states) == 1
        states = states[0]

        gh_model = GroundhogModel(generator, cost)
        gh_model.properties.append(
            ('bpc', cost.actual_cost() * numpy.log(2) / seq_len))
        gh_model.properties.append(('mean_init_state', init_states.mean()))
        gh_model.properties.append(('reset', reset))
        if not args.reset:
            gh_model.updates.append((init_states, states[-1]))

        state = GroundhogState(args.prefix, batch_size,
                               learning_rate=0.0001).as_dict()
        changes = eval("dict({})".format(args.state))
        state.update(changes)

        def output_format(x, y, reset):
            return dict(x=x[:, None], reset=reset)
        train, valid, test = [
            LMIterator(batch_size=batch_size,
                       use_infinite_loop=mode == 'train',
                       path=args.path,
                       seq_len=seq_len,
                       mode=mode,
                       chunks='chars',
                       output_format=output_format,
                       can_fit=True)
            for mode in ['train', 'valid', 'test']]

        trainer = SGD(gh_model, state, train)
        state['on_nan'] = 'warn'
        state['cutoff'] = 1.

        main_loop = MainLoop(train, valid, None, gh_model,
                             trainer, state, None)
        if not args.restart:
            main_loop.load()
        main_loop.main()
    elif args.mode == "sample":
        load_params(generator,  args.prefix + "model.npz")

        chars = numpy.load(args.dict)['unique_chars']

        sample = ComputationGraph(generator.generate(
            n_steps=args.steps, batch_size=10, iterate=True)).function()

        states, outputs, costs = sample()

        for i in range(10):
            print("Generation cost: {}".format(costs[:, i].sum()))
            print("".join([chars[o] for o in outputs[:, i]]))
    else:
        assert False


if __name__ == "__main__":
    main()

# This is the RNNsearch model
from collections import Counter
import argparse
import importlib
import logging
import pprint
from theano import tensor
from toolz import merge
from picklable_itertools.extras import equizip

from blocks.algorithms import (GradientDescent, StepClipping, AdaDelta,
                               CompositeRule, RemoveNotFinite)
from blocks.dump import MainLoopDumpManager
from blocks.filter import VariableFilter
from blocks.main_loop import MainLoop
from blocks.model import Model
from blocks.graph import ComputationGraph, apply_noise, apply_dropout
from blocks.initialization import IsotropicGaussian, Orthogonal, Constant
from blocks.extensions import Printing
from blocks.extensions.monitoring import TrainingDataMonitoring
from blocks.extensions.saveload import LoadFromDump, Dump
from blocks.extensions.plot import Plot

from blocks.bricks import (Tanh, Maxout, Linear, FeedforwardSequence,
                           Bias, Initializable, MLP)
from blocks.bricks.attention import SequenceContentAttention
from blocks.bricks.base import application
from blocks.bricks.lookup import LookupTable
from blocks.bricks.parallel import Fork
from blocks.bricks.recurrent import GatedRecurrent, Bidirectional
from blocks.select import Selector
from blocks.bricks.sequence_generators import (
    LookupFeedback, Readout, SoftmaxEmitter,
    SequenceGenerator
)

import config

from sampling import BleuValidator, Sampler

logger = logging.getLogger(__name__)

# Get the arguments
parser = argparse.ArgumentParser()
parser.add_argument("--proto",  default="get_config_wmt15_fi_en_40k",
                    help="Prototype config to use for config")
parser.add_argument("--subtensor-fix",  action='store_true',
                    help="Speed up training by fixing Theano issue #2219")
args = parser.parse_args()

# Make config global, nasty workaround since parameterizing stream
# will cause erroneous picklable behaviour, find a better solution
config = getattr(config, args.proto)()


# Helper class
class InitializableFeedforwardSequence(FeedforwardSequence, Initializable):
    pass


class MainLoopDumpManagerWMT15(MainLoopDumpManager):

    def load_to(self, main_loop):
        """Loads the dump from the root folder into the main loop.

        Only difference from super().load_to is the exception handling
        for each step separately.
        """
        try:
            logger.info("Loading model parameters...")
            params = self.load_parameters()
            main_loop.model.set_param_values(params)
            for p, v in params.iteritems():
                logger.info("Loaded {:15}: {}".format(v.shape, p))
            logger.info("Number of parameters loaded: {}".format(len(params)))
        except Exception as e:
            logger.error("Error {0}".format(str(e)))

        try:
            logger.info("Loading iteration state...")
            main_loop.iteration_state = self.load_iteration_state()
        except Exception as e:
            logger.error("Error {0}".format(str(e)))

        try:
            logger.info("Loading log...")
            main_loop.log = self.load_log()
        except Exception as e:
            logger.error("Error {0}".format(str(e)))


class LoadFromDumpWMT15(LoadFromDump):
    """Wrapper to use MainLoopDumpManagerWMT15"""

    def __init__(self, config_path, **kwargs):
        super(LoadFromDumpWMT15, self).__init__(config_path, **kwargs)
        self.manager = MainLoopDumpManagerWMT15(config_path)


class LookupFeedbackWMT15(LookupFeedback):

    @application
    def feedback(self, outputs):
        assert self.output_dim == 0

        shp = [outputs.shape[i] for i in xrange(outputs.ndim)]
        outputs_flat = outputs.flatten()
        outputs_flat_zeros = tensor.switch(outputs_flat < 0, 0,
                                           outputs_flat)

        lookup_flat = tensor.switch(outputs_flat[:, None] < 0,
                      tensor.alloc(0., outputs_flat.shape[0], self.feedback_dim),
                      self.lookup.apply(outputs_flat_zeros))
        lookup = lookup_flat.reshape(shp+[self.feedback_dim])
        return lookup


class BidirectionalWMT15(Bidirectional):

    @application
    def apply(self, forward_dict, backward_dict):
        """Applies forward and backward networks and concatenates outputs."""
        forward = self.children[0].apply(as_list=True, **forward_dict)
        backward = [x[::-1] for x in
                    self.children[1].apply(reverse=True, as_list=True,
                                           **backward_dict)]
        return [tensor.concatenate([f, b], axis=2)
                for f, b in equizip(forward, backward)]


class BidirectionalEncoder(Initializable):
    def __init__(self, vocab_size, embedding_dim, state_dim, **kwargs):
        super(BidirectionalEncoder, self).__init__(**kwargs)
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.state_dim = state_dim

        self.lookup = LookupTable(name='embeddings')
        self.bidir = BidirectionalWMT15(GatedRecurrent(activation=Tanh(), dim=state_dim))
        self.fwd_fork = Fork([name for name in self.bidir.prototype.apply.sequences
                          if name != 'mask'], prototype=Linear(), name='fwd_fork')
        self.back_fork = Fork([name for name in self.bidir.prototype.apply.sequences
                          if name != 'mask'], prototype=Linear(), name='back_fork')

        self.children = [self.lookup, self.bidir, self.fwd_fork, self.back_fork]

    def _push_allocation_config(self):
        self.lookup.length = self.vocab_size
        self.lookup.dim = self.embedding_dim

        self.fwd_fork.input_dim = self.embedding_dim
        self.fwd_fork.output_dims = [self.bidir.children[0].get_dim(name)
                                 for name in self.fwd_fork.output_names]
        self.back_fork.input_dim = self.embedding_dim
        self.back_fork.output_dims = [self.bidir.children[1].get_dim(name)
                                 for name in self.back_fork.output_names]

    @application(inputs=['source_sentence', 'source_sentence_mask'],
                 outputs=['representation'])
    def apply(self, source_sentence, source_sentence_mask):
        # Time as first dimension
        source_sentence = source_sentence.T
        source_sentence_mask = source_sentence_mask.T

        embeddings = self.lookup.apply(source_sentence)

        representation = self.bidir.apply(
            merge(self.fwd_fork.apply(embeddings, as_dict=True),
                  {'mask': source_sentence_mask}),
            merge(self.back_fork.apply(embeddings, as_dict=True),
                  {'mask': source_sentence_mask})
        )
        return representation


class GRUInitialState(GatedRecurrent):
    def __init__(self, attended_dim, **kwargs):
        super(GRUInitialState, self).__init__(**kwargs)
        self.attended_dim = attended_dim
        self.initial_transformer = MLP(activations=[Tanh()],
                                       dims=[attended_dim, self.dim],
                                       name='state_initializer')
        self.children.append(self.initial_transformer)

    @application
    def initial_state(self, state_name, batch_size, *args, **kwargs):
        attended = kwargs['attended']
        if state_name == 'states':
            initial_state = self.initial_transformer.apply(
                attended[0, :, -self.attended_dim:])
            return initial_state
        return super(GRUInitialState, self).initial_state(state_name, batch_size, *args, **kwargs)


class Decoder(Initializable):
    def __init__(self, vocab_size, embedding_dim, state_dim,
                 representation_dim, **kwargs):
        super(Decoder, self).__init__(**kwargs)
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.state_dim = state_dim
        self.representation_dim = representation_dim

        self.transition = GRUInitialState(
            attended_dim=state_dim, dim=state_dim,
            activation=Tanh(), name='decoder')
        self.attention = SequenceContentAttention(
            state_names=self.transition.apply.states,
            attended_dim=representation_dim,
            match_dim=state_dim, name="attention")

        readout = Readout(
            source_names=['states', 'feedback', self.attention.take_glimpses.outputs[0]],
            readout_dim=self.vocab_size,
            emitter=SoftmaxEmitter(initial_output=-1),
            feedback_brick=LookupFeedbackWMT15(vocab_size, embedding_dim),
            post_merge=InitializableFeedforwardSequence(
                [Bias(dim=state_dim, name='maxout_bias').apply,
                 Maxout(num_pieces=2, name='maxout').apply,
                 Linear(input_dim=state_dim / 2, output_dim=embedding_dim,
                        use_bias=False, name='softmax0').apply,
                 Linear(input_dim=embedding_dim, name='softmax1').apply]),
            merged_dim=state_dim)

        self.sequence_generator = SequenceGenerator(
            readout=readout,
            transition=self.transition,
            attention=self.attention,
            fork=Fork([name for name in self.transition.apply.sequences
                       if name != 'mask'], prototype=Linear())
        )

        self.children = [self.sequence_generator]

    @application(inputs=['representation', 'source_sentence_mask',
                         'target_sentence_mask', 'target_sentence'],
                 outputs=['cost'])
    def cost(self, representation, source_sentence_mask,
             target_sentence, target_sentence_mask):

        source_sentence_mask = source_sentence_mask.T
        target_sentence = target_sentence.T
        target_sentence_mask = target_sentence_mask.T

        # Get the cost matrix
        cost = self.sequence_generator.cost_matrix(
                    **{'mask': target_sentence_mask,
                       'outputs': target_sentence,
                       'attended': representation,
                       'attended_mask': source_sentence_mask}
        )

        return (cost * target_sentence_mask).sum() / target_sentence_mask.shape[1]

    @application
    def generate(self, source_sentence, representation):
        return self.sequence_generator.generate(
            n_steps=2 * source_sentence.shape[1],
            batch_size=source_sentence.shape[0],
            attended=representation,
            attended_mask=tensor.ones(source_sentence.shape).T)


def main(config, tr_stream, dev_stream):

    # Create Theano variables
    source_sentence = tensor.lmatrix('source')
    source_sentence_mask = tensor.matrix('source_mask')
    target_sentence = tensor.lmatrix('target')
    target_sentence_mask = tensor.matrix('target_mask')
    sampling_input = tensor.lmatrix('input')

    # Construct model
    encoder = BidirectionalEncoder(config['src_vocab_size'], config['enc_embed'],
                                   config['enc_nhids'])
    decoder = Decoder(config['trg_vocab_size'], config['dec_embed'],
                      config['dec_nhids'], config['enc_nhids'] * 2)
    cost = decoder.cost(encoder.apply(source_sentence, source_sentence_mask),
                        source_sentence_mask, target_sentence, target_sentence_mask)

    # Initialize model
    encoder.weights_init = decoder.weights_init = IsotropicGaussian(config['weight_scale'])
    encoder.biases_init = decoder.biases_init = Constant(0)
    encoder.push_initialization_config()
    decoder.push_initialization_config()
    encoder.bidir.prototype.weights_init = Orthogonal()
    decoder.transition.weights_init = Orthogonal()
    encoder.initialize()
    decoder.initialize()

    cg = ComputationGraph(cost)

    # apply dropout for regularization
    if config['dropout'] < 1.0:
        # dropout is applied to the output of maxout in ghog
        dropout_inputs = [x for x in cg.intermediary_variables
                          if x.name == 'maxout_apply_output']
        cg = apply_dropout(cg, dropout_inputs, config['dropout'])

    # Apply weight noise for regularization
    if config['weight_noise_ff'] > 0.0:
        enc_params = Selector(encoder.lookup).get_params().values()
        enc_params += Selector(encoder.fwd_fork).get_params().values()
        enc_params += Selector(encoder.back_fork).get_params().values()
        dec_params = Selector(decoder.sequence_generator.readout).get_params().values()
        dec_params += Selector(decoder.sequence_generator.fork).get_params().values()
        dec_params += Selector(decoder.transition.initial_transformer).get_params().values()
        cg = apply_noise(cg, enc_params+dec_params, config['weight_noise_ff'])

    cost = cg.outputs[0]

    # Print shapes
    shapes = [param.get_value().shape for param in cg.parameters]
    logger.info("Parameter shapes: ")
    for shape, count in Counter(shapes).most_common():
        logger.info('    {:15}: {}'.format(shape, count))
    logger.info("Total number of parameters: {}".format(len(shapes)))

    # Print parameter names
    enc_dec_param_dict = merge(Selector(encoder).get_params(),
                               Selector(decoder).get_params())
    logger.info("Parameter names: ")
    for name, value in enc_dec_param_dict.iteritems():
        logger.info('    {:15}: {}'.format(value.get_value().shape, name))
    logger.info("Total number of parameters: {}".format(len(enc_dec_param_dict)))

    # Set up training algorithm
    if args.subtensor_fix:
        assert config['step_rule'] == 'AdaDelta'
        from subtensor_gradient import GradientDescent_SubtensorFix, AdaDelta_SubtensorFix, subtensor_params
        lookups = subtensor_params(cg, [encoder.lookup, decoder.sequence_generator.readout.feedback_brick.lookup])
        algorithm = GradientDescent_SubtensorFix(
            subtensor_params=lookups,
            cost=cost, params=cg.parameters,
            step_rule=CompositeRule([StepClipping(config['step_clipping']),
                                     RemoveNotFinite(0.9),
                                     AdaDelta_SubtensorFix(subtensor_params=lookups)])
        )
    else:
        algorithm = GradientDescent(
            cost=cost, params=cg.parameters,
            step_rule=CompositeRule([StepClipping(config['step_clipping']),
                                     RemoveNotFinite(0.9),
                                     eval(config['step_rule'])()])
        )

    # Set up beam search and sampling computation graphs
    sampling_representation = encoder.apply(
        sampling_input, tensor.ones(sampling_input.shape))
    generated = decoder.generate(sampling_input, sampling_representation)
    search_model = Model(generated)
    samples, = VariableFilter(
        bricks=[decoder.sequence_generator], name="outputs")(
            ComputationGraph(generated[1]))  # generated[1] is the next_outputs

    # Set up training model
    training_model = Model(cost)

    # Set extensions
    extensions = [
        Sampler(
            model=search_model, config=config, data_stream=tr_stream,
            src_eos_idx=config['src_eos_idx'],
            trg_eos_idx=config['trg_eos_idx'],
            every_n_batches=config['sampling_freq']),
        BleuValidator(
            sampling_input, samples=samples, config=config,
            model=search_model, data_stream=dev_stream,
            src_eos_idx=config['src_eos_idx'],
            trg_eos_idx=config['trg_eos_idx'],
            every_n_batches=config['bleu_val_freq']),
        TrainingDataMonitoring([cost], after_batch=True),
        #Plot('En-Fr', channels=[['decoder_cost_cost']],
        #     after_batch=True),
        Printing(after_batch=True),
        Dump(config['saveto'], every_n_batches=config['save_freq'])
    ]

    # Reload model if necessary
    if config['reload']:
        extensions += [LoadFromDumpWMT15(config['saveto'])]

    # Initialize main loop
    main_loop = MainLoop(
        model=training_model,
        algorithm=algorithm,
        data_stream=tr_stream,
        extensions=extensions
    )

    # Train!
    main_loop.run()


if __name__ == "__main__":
    logger.info("Model options:\n{}".format(pprint.pformat(config)))
    stream = importlib.import_module(config['stream'])
    main(config, stream.masked_stream, stream.dev_stream)



class ReadOnlyDict(dict):

    def __setitem__(self, key, value):
        raise(TypeError, "__setitem__ is not supported")

    def __delitem__(self, key):
        raise(TypeError, "__delitem__ is not supported")

    def update(self, d):
        raise(TypeError, "update is not supported")


def get_states_wmt15_fi_en_40k():
    state = {}

    # Model related
    state['seq_len'] = 50
    state['enc_nhids'] = 100
    state['dec_nhids'] = 100
    state['enc_embed'] = 10
    state['dec_embed'] = 10
    state['prefix'] = 'refBlocks_'

    # Optimization related
    state['batch_size'] = 64

    # Vocabulary related
    state['src_vocab_size'] = 250
    state['trg_vocab_size'] = 250

    # Bleu related
    state['normalized_bleu'] = True
    state['bleu_script'] = '/data/lisatmp3/firatorh/turkishParallelCorpora/iwslt14/scripts/multi-bleu.perl'
    state['val_set'] = '/data/lisatmp3/firatorh/nmt/wmt15/data/fi-en/dev/newsdev2015.tok.seg.fi'
    state['val_set_grndtruth'] = '/data/lisatmp3/firatorh/nmt/wmt15/data/fi-en/dev/newsdev2015.tok.en'
    state['val_set_out'] = 'refBlokcs_adadelta_40k_out.txt'
    state['output_val_set'] = True
    state['beam_size'] = 20

    # Timing related
    state['reload'] = True
    state['save_freq'] = 1
    state['sampling_freq'] = 3
    state['bleu_val_freq'] = 5
    state['val_burn_in'] = 2

    # Monitoring related
    state['hook_samples'] = 3

    return ReadOnlyDict(state)



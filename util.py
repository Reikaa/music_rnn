import os
import math
import numpy as np
import tensorflow as tf    
import cPickle

import midi_util

def prepare_targets(data, unrolled_lengths):
    # roll back the time steps axis to get the target of each example
    targets = np.roll(data, -1, axis=0)
    # targets[-1, :, :] = 0

    # experiment: set the rest of the targets to randoms
    # for seq_idx, length in enumerate(unrolled_lengths):
    #     for i in range(length, targets.shape[0]):
    #         targets[i, seq_idx, :] = np.random.random_sample(targets.shape[2])

    # set the targets to final chord
    for seq_idx, length in enumerate(unrolled_lengths):
        for i in range(length, targets.shape[0]):
            targets[i, seq_idx, :] = 0
            data[i, seq_idx, :] = 0

    # sanity check
    assert targets.shape[0] == data.shape[0]

    return data, targets

def parse_midi_directory(input_dir, time_step):
    files = [ os.path.join(input_dir, f) for f in os.listdir(input_dir)
              if os.path.isfile(os.path.join(input_dir, f)) ] 
    sequences = [ \
        (f, midi_util.parse_midi_to_sequence(f, time_step=time_step)) \
        for f in files ]

    return sequences

def batch_data(sequences, time_batch_len=-1, max_time_batches=-1, verbose=False):
    """
    time_step: dataset-specific time step the MIDI should be broken up into (see parse_midi_to_sequence
               for more details
    time_batch_len: the max unrolling that will take place over BPTT. If -1 then set equal to the length
                    of the longest sequence.
    max_time_batches: the maximum amount of time batches. Every sequence greater than max_time_batches * 
                      time_batch_len is thrown away. If -1, there is no max amount of time batches
    """

    dims = sequences[0].shape[1]
    sequence_lens = [s.shape[0] for s in sequences]
    longest_seq = max(sequence_lens)

    if verbose:
        avg_seq_len = sum(sequence_lens) / len(sequences)
        print "Average Sequence Length: {}".format(avg_seq_len)
        print "Max Sequence Length: {}".format(time_batch_len)
        print "Number of sequences: {}".format(len(sequences))

    if time_batch_len < 0:
        time_batch_len = longest_seq

    total_time_batches = int(math.ceil(float(longest_seq)/float(time_batch_len)))

    if max_time_batches >= 0:
        num_time_batches = min(total_time_batches, max_time_batches)
        max_len = time_batch_len * num_time_batches
        # filter out any sequences that are too long. 
        # TODO: is this the right call, or should we just use the first part of the
        # sequence?
        sequences = filter(lambda x: len(x) <= max_len, sequences)
        sequence_lens = [s.shape[0] for s in sequences]
    else:
        num_time_batches = total_time_batches

    if verbose:
        print "Number of time batches: {}".format(num_time_batches)
        print "Number of sequences after filtering: {}".format(len(sequences))

    unsplit = list()
    unrolled_lengths = list()
    for sequence in sequences:
        # subtract one from each length because we can only perform
        # target matching for the first n-1
        unrolled_lengths.append(sequence.shape[0] - 1)
        copy = sequence.copy()
        copy.resize((time_batch_len * num_time_batches, dims)) 
        unsplit.append(copy)

    stacked = np.dstack(unsplit)
    # swap axes so that shape is (SEQ_LENGTH X BATCH_SIZE X INPUT_DIM)
    all_batches = np.swapaxes(stacked, 1, 2)
    all_batches, all_targets = prepare_targets(all_batches, unrolled_lengths)

    # sanity checks
    assert all_batches.shape == all_targets.shape
    assert all_batches.shape[1] == len(sequences)
    assert all_batches.shape[2] == dims

    batches = np.split(all_batches, [j * time_batch_len for j in range(1, num_time_batches)], axis=0)
    targets = np.split(all_targets, [j * time_batch_len for j in range(1, num_time_batches)], axis=0)

    assert len(batches) == len(targets) == num_time_batches

    rolled_lengths = [list() for i in range(num_time_batches)]
    for length in unrolled_lengths: 
        for time_step in range(num_time_batches): 
            step = time_step * time_batch_len
            if length <= step:
                rolled_lengths[time_step].append(0)
            else:
                rolled_lengths[time_step].append(min(time_batch_len, length - step))

    return batches, targets, rolled_lengths, unrolled_lengths, 

def load_data(data_dir, time_step, time_batch_len, max_time_batches, nottingham=None):

    data = {}

    if nottingham:
        pickle = nottingham

    for dataset in ['train', 'test', 'valid']:

        if nottingham:
            sequences = pickle[dataset]
            metadata = pickle[dataset + '_metadata']
            # sequences = [pickle[dataset][0]]
        else:
            sf = parse_midi_directory(os.path.join(data_dir, dataset), time_step)
            sequences = [s[1] for s in sf]
            files = [s[0] for s in sf]
            # TODO: update metadata of normal method
            metadata = [{
                'path': f,
                'name': f.split("/")[-1].split(".")[0]
            } for f in files]

        if dataset == 'test':
            mtb = -1
        else:
            mtb = max_time_batches

        notes, targets, seq_lengths, unrolled_lengths = batch_data(sequences, time_batch_len, mtb)

        data[dataset] = {
            "data": notes,
            "metadata": metadata,
            "targets": targets,
            "seq_lengths": seq_lengths,
            "unrolled_lengths": unrolled_lengths,
            "time_batch_len": time_batch_len
        }

        data["input_dim"] = notes[0].shape[2]

    return data

def run_epoch(session, model, data, training=False, testing=False, batch_size=100):

    # change each data into a batch of data if it isn't already
    for n in ["data", "targets", "seq_lengths"]:
        if not isinstance(data[n], list):
            data[n] = [ data[n] ]

    target_tensors = [model.loss, model.final_state]
    if testing:
        target_tensors.append(model.probs)
        prob_vals = list()
    if training:
        target_tensors.append(model.train_step)

    state = model.initial_state.eval()
    loss = 0
    for t in range(len(data["data"])):
        results = session.run(
            target_tensors,
            feed_dict={
                model.initial_state: state,
                model.seq_input: data["data"][t],
                model.seq_targets: data["targets"][t],
                model.seq_input_lengths: data["seq_lengths"][t],
                model.unrolled_lengths: data["unrolled_lengths"]
            })

        loss += results[0]
        state = results[1]
        if testing:
            prob_vals.append(results[2])

    if testing:
        return [loss, prob_vals]
    else:
        return loss

def accuracy(raw_probs, raw_targets, unrolled_lengths, config, num_samples=20):
    
    batch_size = config["batch_size"]
    time_batch_len = config["time_batch_len"]
    input_dim = config["input_dim"]

    # reshape probability batches into [time_batch_len * max_time_batches, batch_size, input_dim]
    test_probs = np.concatenate(raw_probs, axis=0)
    test_targets = np.concatenate(raw_targets, axis=0)

    false_positives, false_negatives, true_positives = 0, 0, 0 
    for seq_idx in range(test_targets.shape[1]):
        for step_idx in range(test_targets.shape[0]):
            # can't predict anything with first step
            if step_idx == 0:
                continue

            # if we've reached the end of the sequence, go to next seq
            if step_idx >= unrolled_lengths[seq_idx]:
                break

            for note_idx, prob in enumerate(test_probs[step_idx-1, seq_idx, :]):  
                num_occurrences = np.random.binomial(num_samples, prob)
                if test_targets[step_idx, seq_idx, note_idx] == 0.0:
                    false_positives += num_occurrences
                else:
                    false_negatives += (num_samples - num_occurrences)
                    true_positives += num_occurrences
                
    accuracy = (float(true_positives) / (true_positives + false_positives + false_negatives)) 

    print "Accuracy: {}".format(accuracy)

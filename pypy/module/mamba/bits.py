import operator

from rpython.rlib import jit
from rpython.rlib.rarithmetic import intmask
from rpython.rlib.rbigint     import rbigint, SHIFT, NULLDIGIT, ONERBIGINT, \
                                     NULLRBIGINT, _store_digit, _x_int_sub, \
                                     _widen_digit, BASE16
from rpython.tool.sourcetools import func_renamer, func_with_new_name

from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.gateway import WrappedDefault, interp2app, interpindirect2app, unwrap_spec
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.typedef import TypeDef, GetSetProperty
from pypy.objspace.std.intobject import W_IntObject, wrapint, ovfcheck, _hash_int
from pypy.objspace.std.longobject import W_LongObject, newlong, _hash_long
from pypy.objspace.std.sliceobject import W_SliceObject
from pypy.objspace.std.util import COMMUTATIVE_OPS

# Shunning:
# I don't wrap int/long anymore, just keep rbigint all the time
# Currently we only support arithmetic operations between Bits only, so
# really those radd/rsub methods don't take effect.

# NOTE that we should keep self.value positive after any computation:
# - The sign of the rbigint field should always be one
# - Always AND integer value with mask, never store any negative int
# * Performing rbigint.and_/rbigint.int_and_ will turn sign back to 1
# - rbigint._normalize() can only be called in @jit.elidable funcs

mask = rbigint([NULLDIGIT], 1, 1)
LONG_MASKS = [ mask ]
for i in xrange(512):
  mask = mask.int_mul(2).int_add( 1 )
  LONG_MASKS.append( mask )

def get_long_mask( i ):
  return LONG_MASKS[ i ]
get_long_mask._always_inline_ = True

def get_int_mask( i ):
  return int((1<<i) - 1)
get_int_mask._always_inline_ = True

@jit.elidable
def int_bit_length( val ):
  bits = 0
  if val < 0:
    val = -((val + 1) >> 1)
    bits = 1
  while val:
    bits += 1
    val >>= 1
  return bits

#-------------------------------------------------------------------------
# Shunning: The following functions are specialized implementations for
# Bits arithmetics. Basically we squash arithmetic ops and ANDing mask to
# the same function to avoid copying and also reduce the constant factor
# based on the return type (int/rbigint).
#-------------------------------------------------------------------------

cmp_opp = {
  'lt': 'gt',
  'le': 'ge',
  'eq': 'eq', # commutative
  'ne': 'ne', # commutative
  'gt': 'lt',
  'ge': 'le',
}

# This function implements fast ANDing mask functionality. PLEASE ONLY USE
# IT ON POSITIVE RBIGINT. On the other hand, AND get_long_mask for those
# operators that might produce negative results such as sub/new
@jit.elidable
def _rbigint_maskoff_high( value, masklen ):
  if not value.sign:  return value
  # assert value.sign > 0

  lastword = (masklen - 1)/SHIFT
  masksize  = lastword + 1
  if masksize > value.numdigits(): return value
  assert masksize > 0 # tell the dumb translator
  # From now on 0 < masksize <= value.numdigits(), so lastword exists

  ret = rbigint(value._digits[:masksize], 1, masksize)

  # Here, if masklen % SHIFT == 0, then we don't need to mask the last
  # word because wordpos = masklen / SHIFT = masksize in this case
  maskbit = masklen % SHIFT
  if maskbit != 0:
    lastdigit = ret.digit(lastword)
    mask = get_int_mask(maskbit)
    if lastdigit >= mask:
      ret.setdigit( lastword, lastdigit & mask )

  ret._normalize()
  return ret

# This function implements rshift between two rbigints
@jit.elidable
def _rbigint_rshift( value, shamt ):
  if not value.sign or not shamt.sign:  return value
  if shamt.numdigits() > 1: return NULLRBIGINT
  shamt = shamt.digit(0)

  wordshift = shamt / SHIFT
  newsize = value.numdigits() - wordshift
  if newsize <= 0:  return NULLRBIGINT

  loshift = shamt - wordshift*SHIFT
  hishift = SHIFT - loshift
  ret = rbigint([NULLDIGIT]*newsize, 1, newsize)

  i = 0
  lastidx = newsize - 1
  curword = value.digit(wordshift)
  while i < lastidx:
    newdigit  = curword >> loshift
    wordshift = wordshift + 1
    curword   = value.digit(wordshift)
    ret.setdigit(i, newdigit | (curword << hishift) )
    i += 1
  # last digit
  ret.setdigit(i, curword >> loshift)

  ret._normalize()
  return ret
_rbigint_rshift._always_inline_ = 'try' # It's so fast that it's always benefitial.

# This function implements getslice functionality that returns rbigint.
@jit.elidable
def _rbigint_rshift_maskoff( value, shamt, masklen ):
  if not value.sign:  return value
  # shamt must be > 0, value.sign must > 0
  if shamt == 0: return _rbigint_maskoff_high( value, masklen )

  wordshift = shamt / SHIFT
  oldsize   = value.numdigits()
  if oldsize <= wordshift:  return NULLRBIGINT

  newsize  = oldsize - wordshift
  masksize = (masklen - 1)/SHIFT + 1
  retsize  = min( newsize, masksize )

  loshift = shamt - wordshift*SHIFT
  hishift = SHIFT - loshift
  ret = rbigint( [NULLDIGIT] * retsize, 1, retsize )
  i = 0
  while i < retsize:
    newdigit = (value.digit(wordshift) >> loshift)
    if i+1 < newsize:
      newdigit |= (value.digit(wordshift+1) << hishift)
    ret.setdigit(i, newdigit)
    i += 1
    wordshift += 1

  if masksize <= retsize:
    maskbit = masklen % SHIFT
    if maskbit != 0:
      lastword  = i - 1
      lastdigit = ret.digit(lastword)
      mask      = get_int_mask(maskbit)
      if lastdigit >= mask:
        ret.setdigit( lastword, lastdigit & mask )

  ret._normalize()
  return ret
_rbigint_rshift_maskoff._always_inline_ = True

# This function implements getslice functionality that returns normal int
# Shunning: This is FASTER than calling the above func and do x.digit(0)
@jit.elidable
def _rbigint_rshift_maskoff_retint( value, shamt, masklen ):
  # assert masklen <= SHIFT
  if not value.sign:  return 0
  # shamt must be > 0, value.sign must > 0
  if shamt == 0:
    return value.digit(0) & get_int_mask(masklen)

  wordshift = shamt / SHIFT
  oldsize   = value.numdigits()
  if oldsize <= wordshift:  return 0

  newsize = oldsize - wordshift
  loshift = shamt - wordshift*SHIFT
  hishift = SHIFT - loshift
  ret = value.digit(wordshift) >> loshift
  if newsize > 1:
    ret |= value.digit(wordshift+1) << hishift
  ret &= get_int_mask(masklen)
  return ret

_rbigint_rshift_maskoff_retint._always_inline_ = True

# This function implements getidx functionality.
@jit.elidable
def _rbigint_getidx( value, index ):
  wordpos = index / SHIFT
  if wordpos > value.numdigits(): return 0
  bitpos  = index - wordpos*SHIFT
  return (value.digit(wordpos) >> bitpos) & 1
_rbigint_getidx._always_inline_ = True

# This function implements setitem functionality.
@jit.elidable
def _rbigint_setidx( value, index, other ):
  size    = value.numdigits()
  wordpos = index / SHIFT

  if wordpos >= size:
    if not other:
      return value

    bitpos  = index - wordpos*SHIFT
    shift   = 1 << bitpos
    return rbigint( value._digits[:size] + \
                    [NULLDIGIT]*(wordpos-size) + \
                    [_store_digit(shift)], 1, wordpos + 1 )

  # wordpos < size
  digit = value.digit(wordpos)
  bitpos  = index - wordpos*SHIFT
  shift   = 1 << bitpos

  if other == 1:
    if digit & shift: # already 1
      return value
    # the value is changed
    ret = rbigint( value._digits[:size], 1, size )
    ret.setdigit( wordpos, digit | shift )
    return ret
  # other == 0

  if not (digit & shift): # already 0
    return value
  # the value is changed
  digit ^= shift
  if digit == 0 and wordpos == size-1:
    assert wordpos >= 0
    return rbigint( value._digits[:wordpos] + [NULLDIGIT], 1, wordpos )

  ret = rbigint(value._digits[:size], 1, size)
  ret.setdigit( wordpos, digit )
  return ret

# This function implements lshift+AND mask functionality.
# PLEASE NOTE THAT value should be from Bits.value, and masklen should
# be larger than this Bits object's nbits
@jit.elidable
def _rbigint_lshift_maskoff( value, shamt, masklen ):
  if not value.sign or not shamt: return value
  if shamt >= masklen:  return NULLRBIGINT
  assert shamt > 0
  # shamt must > 0, value.sign must >= 0

  wordshift = shamt // SHIFT
  remshift  = shamt - wordshift * SHIFT

  oldsize   = value.numdigits()

  maskbit   = masklen % SHIFT
  masksize  = (masklen - 1)/SHIFT + 1

  if not remshift:
    retsize = min( oldsize + wordshift, masksize )
    ret = rbigint( [NULLDIGIT]*retsize, 1, retsize )
    j = 0
    while j < oldsize and wordshift < retsize:
      ret.setdigit( wordshift, value.digit(j) )
      wordshift += 1
      j += 1

    if wordshift == masksize and maskbit != 0:
      lastword = retsize - 1
      ret.setdigit( lastword, ret.digit(lastword) & get_int_mask(maskbit) )

    ret._normalize()
    return ret

  newsize  = oldsize + wordshift + 1

  if masksize < newsize:
    retsize = masksize

    ret = rbigint([NULLDIGIT]*retsize, 1, retsize)
    accum = _widen_digit(0)
    j = 0
    while j < oldsize and wordshift < retsize:
      accum += value.widedigit(j) << remshift
      ret.setdigit(wordshift, accum)
      accum >>= SHIFT
      wordshift += 1
      j += 1

    # no accum
    if maskbit != 0:
      lastword  = retsize - 1
      lastdigit = ret.digit(lastword)
      mask      = get_int_mask(maskbit)
      if lastdigit >= mask:
        ret.setdigit( lastword, lastdigit & mask )

    ret._normalize()
    return ret

  # masksize >= newsize
  retsize = newsize

  ret = rbigint([NULLDIGIT]*retsize, 1, retsize)
  accum = _widen_digit(0)
  j = 0
  while j < oldsize and wordshift < retsize:
    accum += value.widedigit(j) << remshift
    ret.setdigit(wordshift, accum)
    accum >>= SHIFT
    wordshift += 1
    j += 1

  if masksize == newsize and maskbit != 0:
    accum &= get_int_mask(maskbit)

  ret.setdigit(wordshift, accum)

  ret._normalize()
  return ret

# setitem helpers

# Must return rbigint that cannot fit into int
@jit.elidable
def setitem_long_long_helper( value, other, start, stop ):
  if other.numdigits() <= 1:
    return setitem_long_int_helper( value, other.digit(0), start, stop )

  if other.sign < 0:
    slice_nbits = stop - start
    other = other.and_( get_long_mask(slice_nbits) )
    if other.numdigits() == 1:
      return setitem_long_int_helper( value, other.digit(0), start, stop )

  vsize = value.numdigits()
  other = other.lshift( start ) # lshift first to align two rbigints
  osize = other.numdigits()

  # Now other must be long, wordstart must < wordstop
  wordstart = start / SHIFT

  # vsize <= wordstart < wordstop, concatenate
  if wordstart >= vsize:
    return rbigint(value._digits[:vsize] + other._digits[vsize:], 1, osize )

  wordstop = stop / SHIFT # wordstop >= osize-1
  # (wordstart <) wordstop < vsize
  if wordstop < vsize:
    ret = rbigint( value._digits[:vsize], 1, vsize )

    # do start
    bitstart = start - wordstart*SHIFT
    tmpstart = other.digit( wordstart ) | (ret.digit(wordstart) & get_int_mask(bitstart))
    # if bitstart:
      # tmpstart |= ret.digit(wordstart) & get_int_mask(bitstart) # lo
    ret.setdigit( wordstart, tmpstart )

    i = wordstart+1

    if osize < wordstop:
      while i < osize:
        ret.setdigit( i, other.digit(i) )
        i += 1
      while i < wordstop:
        ret._digits[i] = NULLDIGIT
        i += 1
    else: # osize >= wordstop
      while i < wordstop:
        ret.setdigit( i, other.digit(i) )
        i += 1

    # do stop
    bitstop  = stop - wordstop*SHIFT
    if bitstop:
      masked_val = ret.digit(wordstop) & ~get_int_mask(bitstop) #hi
      ret.setdigit( wordstop, other.digit(wordstop) | masked_val ) # lo|hi

    return ret

  assert wordstart >= 0
  # wordstart < vsize <= wordstop
  ret = rbigint( value._digits[:wordstart] + \
                 other._digits[wordstart:osize], 1, osize )

  # do start
  bitstart = start - wordstart*SHIFT
  if bitstart:
    masked_val = value.digit(wordstart) & get_int_mask(bitstart) # lo
    ret.setdigit( wordstart, masked_val | ret.digit(wordstart) ) # lo | hi

  return ret

@jit.elidable
def setitem_long_int_helper( value, other, start, stop ):
  vsize = value.numdigits()
  if other < 0:
    slice_nbits = stop - start
    if slice_nbits < SHIFT:
      other &= get_int_mask(slice_nbits)
    else:
      tmp = get_long_mask(slice_nbits).int_and_( other )
      return setitem_long_long_helper( value, tmp, start, stop )

  # wordstart must < wordstop
  wordstart = start / SHIFT
  bitstart  = start - wordstart*SHIFT

  # vsize <= wordstart < wordstop, concatenate
  if wordstart >= vsize:
    if not other: return value # if other is zero, do nothing

    if not bitstart: # aha, not chopped into two parts
      return rbigint( value._digits[:vsize] + \
                      [NULLDIGIT]*(wordstart-vsize) + \
                      [_store_digit(other)], 1, wordstart+1 )

    # split into two parts
    lo = SHIFT-bitstart
    val1 = other & get_int_mask(lo)
    if val1 == other: # aha, the higher part is zero
      return rbigint( value._digits[:vsize] + \
                      [NULLDIGIT]*(wordstart-vsize) + \
                      [_store_digit(val1 << bitstart)], 1, wordstart+1 )
    return rbigint( value._digits[:vsize] + \
                    [NULLDIGIT]*(wordstart-vsize) + \
                    [_store_digit(val1 << bitstart)] + \
                    [_store_digit(other >> lo)], 1, wordstart+2 )

  wordstop = stop / SHIFT
  bitstop  = stop - wordstop*SHIFT
  # (wordstart <=) wordstop < vsize
  if wordstop < vsize:
    ret = rbigint( value._digits[:vsize], 1, vsize )
    maskstop = get_int_mask(bitstop)
    valstop  = ret.digit(wordstop)

    if wordstop == wordstart: # valstop is ret.digit(wordstart)
      valuemask = ~(maskstop - get_int_mask(bitstart))
      ret.setdigit( wordstop, (valstop & valuemask) | (other << bitstart) )

    # span multiple words
    # wordstart < wordstop
    else:
      # do start
      if not bitstart:
        ret.setdigit( wordstart, other )
        i = wordstart + 1
        while i < wordstop:
          ret._digits[i] = NULLDIGIT
          i += 1
        ret.setdigit( wordstop, valstop & ~maskstop )
      else:
        lo = SHIFT-bitstart
        val1 = other & get_int_mask(lo)
        word = (ret.digit(wordstart) & get_int_mask(bitstart)) | (val1 << bitstart)
        ret.setdigit( wordstart, word )

        val2 = other >> lo
        i = wordstart + 1
        if i == wordstop:
          ret.setdigit( i, val2 | (valstop & ~maskstop) )
        else: # i < wordstop
          ret.setdigit( i, val2 )
          i += 1
          while i < wordstop:
            ret._digits[i] = NULLDIGIT
            i += 1
          ret.setdigit( wordstop, valstop & ~maskstop )
    ret._normalize()
    return ret

  # wordstart < vsize <= wordstop, highest bits will be cleared
  newsize = wordstart + 2 #
  assert wordstart >= 0
  ret = rbigint( value._digits[:wordstart] + \
                [NULLDIGIT, NULLDIGIT], 1, newsize )

  bitstart = start - wordstart*SHIFT
  if not bitstart:
    ret.setdigit( wordstart, other )
  else:
    lo = SHIFT-bitstart
    val1 = other & get_int_mask(lo)
    word = (value.digit(wordstart) & get_int_mask(bitstart)) | (val1 << bitstart)
    ret.setdigit( wordstart, word )

    if val1 != other:
      ret.setdigit( wordstart+1, other >> lo )

  ret._normalize()
  return ret

setitem_long_int_helper._always_inline_ = True

class W_Bits(W_Root):
  __slots__ = ( "nbits", "intval", "bigval" )
  _immutable_fields_ = [ "nbits" ]

  def __init__( self, nbits, intval=0, bigval=None ):
    self.nbits  = nbits
    self.intval = intval
    self.bigval = bigval

  def descr_copy(self):
    return W_Bits( self.nbits, self.intval, self.bigval )

  def descr_deepcopy(self, w_memo):
    return self.descr_copy()

  def descr_clone(self):
    return self.descr_copy()

  # value can be negative! Be extremely cautious with _rb_maskoff_high
  @staticmethod
  @unwrap_spec(w_value=WrappedDefault(0))
  def descr_new( space, w_objtype, w_nbits, w_value ):
    w_obj = space.allocate_instance( W_Bits, w_objtype )

    if type(w_nbits) is W_IntObject:
      nbits = w_nbits.intval
      if nbits < 1 or nbits > 512:
        raise oefmt(space.w_ValueError, "1 <= 'nbits' <= 512, not %d", w_nbits.intval)
      w_obj.nbits = nbits

      if nbits <= SHIFT:
        # w_obj.bigval = None
        mask = get_int_mask(nbits)
        if   isinstance(w_value, W_Bits):
          if w_value.nbits <= SHIFT:
            w_obj.intval = w_value.intval & mask
          else:
            w_obj.intval = w_value.bigval.digit(0) & mask
        elif isinstance(w_value, W_IntObject):
          w_obj.intval = w_value.intval & mask
        elif type(w_value) is W_LongObject:
          w_obj.intval = w_value.num.int_and_( mask ).digit(0)
        else:
          raise oefmt(space.w_TypeError, "Value used to construct Bits%d "
                      "must be int/long/Bits " # or whatever has __int__, "
                      "not '%T'", nbits, w_value)

      else: # nbits > SHIFT
        w_obj.intval = 0
        if   isinstance(w_value, W_Bits):
          if w_value.nbits <= SHIFT:
            w_obj.bigval = rbigint.fromint( w_value.intval )
          else:
            w_obj.bigval = _rbigint_maskoff_high( w_value.bigval, nbits )

        elif isinstance(w_value, W_IntObject):
          w_obj.bigval = get_long_mask(nbits).int_and_( w_value.intval )
        elif type(w_value) is W_LongObject:
          w_obj.bigval = get_long_mask(nbits).and_( w_value.num )
        else:
          raise oefmt(space.w_TypeError, "Value used to construct Bits%d "
                      "must be int/long/Bits" # or whatever has __int__, "
                      "not '%T'", nbits, w_value)

    else:
      raise oefmt(space.w_TypeError, "'nbits' must be an int, not '%T'", w_nbits )

    return w_obj

  def descr_get_nbits(self, space):
    jit.hint( self.nbits, promote=True )
    nbits = wrapint( space, self.nbits )
    return nbits

  #-----------------------------------------------------------------------
  # get/setitem
  #-----------------------------------------------------------------------

  def check_slice_range( self, space, start, stop ):
    if start >= stop:
      raise oefmt(space.w_ValueError, "Invalid range: start [%d] >= stop [%d]", start, stop )
    if start < 0:
      raise oefmt(space.w_ValueError, "Negative start: [%d]", start )
    if stop > self.nbits:
      raise oefmt(space.w_ValueError, "Stop [%d] too big for Bits%d", stop, self.nbits )

  check_slice_range._always_inline_ = True

  def descr_getitem(self, space, w_index):
    if type(w_index) is W_SliceObject:
      if space.is_w(w_index.w_step, space.w_None):
        w_start = w_index.w_start
        start   = 0
        if   isinstance(w_start, W_Bits):
          if w_start.nbits <= SHIFT:
            start = w_start.intval
          else:
            tmp = w_start.bigval
            if tmp.numdigits() > 1:
              raise oefmt(space.w_ValueError, "Index [%s] too big for Bits%d",
                                              rbigint.str(tmp), self.nbits )
            start = tmp.digit(0)
        elif type(w_start) is W_IntObject:
          start = w_start.intval
        elif type(w_start) is W_LongObject:
          start = w_start.num.toint()
        else:
          raise oefmt(space.w_TypeError, "Please pass in int/Bits variables for the slice. -- getitem #1" )

        w_stop = w_index.w_stop
        stop   = 0
        if   isinstance(w_stop, W_Bits):
          if w_stop.nbits <= SHIFT:
            stop = w_stop.intval
          else:
            tmp = w_stop.bigval
            if tmp.numdigits() > 1:
              raise oefmt(space.w_ValueError, "Index [%s] too big for Bits%d",
                                              rbigint.str(tmp), self.nbits )
            stop = tmp.digit(0)
        elif type(w_stop) is W_IntObject:
          stop = w_stop.intval
        elif type(w_stop) is W_LongObject:
          stop = w_stop.num.toint()
        else:
          raise oefmt(space.w_TypeError, "Please pass in int/Bits variables for the slice. -- getitem #2" )

        self.check_slice_range( space, start, stop )
        slice_nbits = stop - start
        if self.nbits <= SHIFT:
          res = (self.intval >> start) & get_int_mask(slice_nbits)
          return W_Bits( slice_nbits, res )
        else:
          if slice_nbits <= SHIFT:
            res = _rbigint_rshift_maskoff_retint( self.bigval, start, slice_nbits )
            return W_Bits( slice_nbits, res )
          else:
            res = _rbigint_rshift_maskoff( self.bigval, start, slice_nbits )
            return W_Bits( slice_nbits, 0, res )

      else:
        raise oefmt(space.w_ValueError, "Bits slice cannot have step." )

    else:
      index = 0
      if isinstance(w_index, W_Bits): # peel the onion
        if w_index.nbits <= SHIFT:
          index = w_index.intval
        else:
          tmp = w_index.bigval
          if tmp.numdigits() > 1:
            raise oefmt(space.w_ValueError, "Index [%s] too big for Bits%d",
                                            rbigint.str(tmp), self.nbits )
          index = tmp.digit(0) # must >= 0
      elif type(w_index) is W_IntObject:
        index = w_index.intval
      elif type(w_index) is W_LongObject:
        index = w_index.num.toint()
        if index < 0:
          raise oefmt(space.w_ValueError, "Negative index: [%d]", index )
      else:
        raise oefmt(space.w_TypeError, "Please pass in int/Bits variables for the slice. -- getitem #3" )

      if index >= self.nbits:
        raise oefmt(space.w_ValueError, "Index [%d] too big for Bits%d", index, self.nbits )

      if self.nbits <= SHIFT:
        return W_Bits( 1, (self.intval >> index) & 1 )
      return W_Bits( 1, _rbigint_getidx( self.bigval, index ) )

  def descr_setitem(self, space, w_index, w_other):
    if type(w_index) is W_SliceObject:
      if space.is_w(w_index.w_step, space.w_None):
        w_start = w_index.w_start
        start   = 0
        if   isinstance(w_start, W_Bits):
          if w_start.nbits <= SHIFT:
            start = w_start.intval
          else:
            tmp = w_start.bigval
            if tmp.numdigits() > 1:
              raise oefmt(space.w_ValueError, "Index [%s] too big for Bits%d",
                                              rbigint.str(tmp), self.nbits )
            start = tmp.digit(0)
        elif type(w_start) is W_IntObject:
          start = w_start.intval
        elif type(w_start) is W_LongObject:
          start = w_start.num.toint()
        else:
          raise oefmt(space.w_TypeError, "Please pass in int/Bits variables for the slice. -- setitem #1" )

        w_stop = w_index.w_stop
        stop   = 0
        if   isinstance(w_stop, W_Bits):
          if w_stop.nbits <= SHIFT:
            stop = w_stop.intval
          else:
            tmp = w_stop.bigval
            if tmp.numdigits() > 1:
              raise oefmt(space.w_ValueError, "Index [%s] too big for Bits%d",
                                              rbigint.str(tmp), self.nbits )
            stop = tmp.digit(0)
        elif isinstance(w_stop, W_IntObject):
          stop = w_stop.intval
        elif type(w_stop) is W_LongObject:
          stop = w_stop.num.toint()
        else:
          raise oefmt(space.w_TypeError, "Please pass in int/Bits variables for the slice. -- setitem #2" )

        self.check_slice_range( space, start, stop )
        slice_nbits = stop - start

        # Check value bitlen. No need to check Bits, but check int/long.

        if self.nbits <= SHIFT: # slice_nbits and w_other.nbits must <= SHIFT

          if isinstance(w_other, W_Bits):
            if w_other.nbits > slice_nbits:
              raise oefmt(space.w_ValueError, "Value of type Bits%d cannot fit into "
                          "[%d:%d](%d-bit) slice", w_other.nbits, start, stop, slice_nbits )
            valuemask   = ~(get_int_mask(slice_nbits) << start)
            self.intval = (self.intval & valuemask) | (w_other.intval << start)

          elif isinstance(w_other, W_IntObject):
            other = w_other.intval
            blen  = int_bit_length( other )
            if blen > slice_nbits:
              raise oefmt(space.w_ValueError, "Value %d cannot fit into "
                    "[%d:%d] (%d-bit) slice", other, start, stop, slice_nbits )

            mask = get_int_mask(slice_nbits)
            other &= mask
            valuemask = ~(mask << start)
            self.intval = (self.intval & valuemask) | (other << start)

          elif type(w_other) is W_LongObject:
            other = w_other.num
            blen = other.bit_length()
            if blen > slice_nbits:
              raise oefmt(space.w_ValueError, "Value %s cannot fit into "
                    "[%d:%d] (%d-bit) slice", rbigint.str(other), start, stop, slice_nbits )
            mask = get_int_mask(slice_nbits)
            other = other.int_and_(mask).digit(0)
            valuemask = ~(mask << start)
            self.intval = (self.intval & valuemask) | (other << start)

        # self.nbits > SHIFT, use bigval
        else:
          if isinstance(w_other, W_Bits):
            if w_other.nbits <= SHIFT:
              self.bigval = setitem_long_int_helper( self.bigval, w_other.intval, start, stop )
            else:
              self.bigval = setitem_long_long_helper( self.bigval, w_other.bigval, start, stop )

          elif isinstance(w_other, W_IntObject):
            other = w_other.intval
            blen  = int_bit_length( other )
            if blen > slice_nbits:
              raise oefmt(space.w_ValueError, "Value %d cannot fit into "
                    "[%d:%d] (%d-bit) slice", other, start, stop, slice_nbits )
            other = get_long_mask(slice_nbits).int_and_( other )
            self.bigval = setitem_long_long_helper( self.bigval, other, start, stop )

          elif type(w_other) is W_LongObject:
            other = w_other.num
            blen = other.bit_length()
            if blen > slice_nbits:
              raise oefmt(space.w_ValueError, "Value %s cannot fit into "
                    "[%d:%d] (%d-bit) slice", rbigint.str(other), start, stop, slice_nbits )

            other = get_long_mask(slice_nbits).and_( other )
            self.bigval = setitem_long_long_helper( self.bigval, other, start, stop )

      else:
        raise oefmt(space.w_ValueError, "Bits slice cannot have step." )

    else:
      index = 0
      if isinstance(w_index, W_Bits): # peel the onion
        if w_index.nbits <= SHIFT:
          index = w_index.intval
        else:
          tmp = w_index.bigval
          if tmp.numdigits() > 1:
            raise oefmt(space.w_ValueError, "Index [%s] too big for Bits%d",
                                            rbigint.str(tmp), self.nbits )
          index = tmp.digit(0) # must >= 0
      elif type(w_index) is W_IntObject:
        index = w_index.intval
        if index < 0:
          raise oefmt(space.w_ValueError, "Negative index: [%d]", index )
      elif type(w_index) is W_LongObject:
        index = w_index.num.toint()
        if index < 0:
          raise oefmt(space.w_ValueError, "Negative index: [%d]", index )
      else:
        raise oefmt(space.w_TypeError, "Please pass in int/Bits variables for the slice. -- setitem #3" )

      if index >= self.nbits:
        raise oefmt(space.w_ValueError, "Index [%d] too big for Bits%d", index, self.nbits )

      # Check value bitlen. No need to check Bits, but check int/long.
      if isinstance(w_other, W_Bits):
        o_nbits = w_other.nbits
        if o_nbits > 1:
          raise oefmt(space.w_ValueError, "Bits%d cannot fit into 1-bit slice", o_nbits )
        other = w_other.intval # must be 1-bit and don't even check

        if self.nbits <= SHIFT:
          self.intval = (self.intval & ~(1 << index)) | (other << index)
        else:
          self.bigval = _rbigint_setidx( self.bigval, index, other )

      elif isinstance(w_other, W_IntObject):
        other = w_other.intval
        if other < 0 or other > 1:
          raise oefmt(space.w_ValueError, "Value %d cannot fit into 1-bit slice", other )

        if self.nbits <= SHIFT:
          self.intval = (self.intval & ~(1 << index)) | (other << index)
        else:
          self.bigval = _rbigint_setidx( self.bigval, index, other )

      elif type(w_other) is W_LongObject:
        other = w_other.num
        if other.numdigits() > 1:
          raise oefmt(space.w_ValueError, "Value %s cannot fit into 1-bit slice", rbigint.str(other) )

        other = other.digit(0)
        if other < 0 or other > 1:
          raise oefmt(space.w_ValueError, "Value %d cannot fit into 1-bit slice", other )

        if self.nbits <= SHIFT:
          self.intval = (self.intval & ~(1 << index)) | (other << index)
        else:
          self.bigval = _rbigint_setidx( self.bigval, index, other )
      else:
        raise oefmt(space.w_TypeError, "Please pass in int/long/Bits value. -- setitem #4" )

  #-----------------------------------------------------------------------
  # Miscellaneous methods for string format
  #-----------------------------------------------------------------------

  def descr_oct(self, space):
    if self.nbits <= SHIFT:
      return space.newtext(oct(self.intval))
    return space.newtext( rbigint.oct(self.bigval) )

  def descr_hex(self, space):
    if self.nbits <= SHIFT:
      return space.newtext(hex(self.intval))
    return space.newtext( rbigint.hex(self.bigval) )

  def _format16(self, space):
    if self.nbits <= SHIFT:
      data = (rbigint.fromint(self.intval)).format(BASE16)
    else:
      data = self.bigval.format(BASE16)
    w_data = space.newtext( data )
    return space.text_w( w_data.descr_zfill(space, (((self.nbits-1)>>2)+1)) )

  def descr_repr(self, space):
    return space.newtext( "Bits%d( 0x%s )" % (self.nbits, self._format16(space)) )

  def descr_str(self, space):
    return space.newtext( "%s" % (self._format16(space)) )

  #-----------------------------------------------------------------------
  # comparators
  #-----------------------------------------------------------------------

  def _make_descr_cmp(opname):
    iiop = getattr( operator, opname )
    llop = getattr( rbigint , opname )
    liop = getattr( rbigint , "int_"+opname )
    ilopp = getattr( rbigint , "int_"+cmp_opp[opname] )

    @func_renamer('descr_' + opname)
    def descr_cmp(self, space, w_other):
      if self.nbits <= SHIFT:
        x = self.intval
        if isinstance(w_other, W_Bits):
          if w_other.nbits <= SHIFT:
            return W_Bits( 1, iiop( x, w_other.intval ) )
          else:
            return W_Bits( 1, ilopp( w_other.bigval, x ) )

        elif isinstance(w_other, W_IntObject):
          # TODO Maybe add int_bit_length check?
          return W_Bits( 1, iiop( x, w_other.intval & get_int_mask(self.nbits) ) )

        elif type(w_other) is W_LongObject:
          # TODO Maybe add bit_length check?
          return W_Bits( 1, ilopp( get_long_mask(self.nbits).and_( w_other.num ), x ) )

      # self.nbits > SHIFT, use bigval
      else:
        x = self.bigval
        if isinstance(w_other, W_Bits):
          if w_other.nbits <= SHIFT:
            return W_Bits( 1, liop( x, w_other.intval ) )
          else:
            return W_Bits( 1, llop( x, w_other.bigval ) )

        elif isinstance(w_other, W_IntObject):
          # TODO Maybe add bit_length check?
          return W_Bits( 1, llop( x, get_long_mask(self.nbits).int_and_( w_other.intval ) ) )

        elif type(w_other) is W_LongObject:
          # TODO Maybe add bit_length check?
          return W_Bits( 1, llop( x, get_long_mask(self.nbits).and_( w_other.num ) ) )

      return W_Bits( 1, 0 )
      # Match cpython behavior
      # raise oefmt(space.w_TypeError, "Please compare two Bits/int/long objects" )

    return descr_cmp

  descr_lt = _make_descr_cmp('lt')
  descr_le = _make_descr_cmp('le')
  descr_eq = _make_descr_cmp('eq')
  descr_ne = _make_descr_cmp('ne')
  descr_gt = _make_descr_cmp('gt')
  descr_ge = _make_descr_cmp('ge')

  #-----------------------------------------------------------------------
  # binary arith ops
  #-----------------------------------------------------------------------
  # Note that we have to check commutativity along with type because
  # rbigint doesn't have "rsub" implementation so we cannot do "int"-"long"

  def _make_descr_binop_opname(opname, ovf=True):
    # Shunning: shouldn't overwrite opname -- "and_" is not in COMMUTATIVE_OPS
    _opn = opname + ('_' if opname in ('and', 'or') else '')
    llop = getattr( rbigint, _opn )
    liop = getattr( rbigint, "int_"+_opn )
    iiop = getattr( operator, _opn )

    @func_renamer('descr_' + opname)
    def descr_binop(self, space, w_other):
      # add, sub, mul
      if ovf:
        if self.nbits <= SHIFT:
          x = self.intval

          if isinstance(w_other, W_Bits):
            if w_other.nbits <= SHIFT: # res_nbits <= SHIFT
              y = w_other.intval
              res_nbits = max(self.nbits, w_other.nbits)
              mask = get_int_mask(res_nbits)
              try:
                z = ovfcheck( iiop(x, y) )
                return W_Bits( res_nbits, z & mask )
              except OverflowError:
                z = liop( rbigint.fromint(x), y )
                if opname in COMMUTATIVE_OPS: # add, mul
                  z = z.digit(0) & mask
                else: # sub, should AND mask
                  z = z.int_and_( mask ).digit(0)
                return W_Bits( res_nbits, z )

            else: # res_nbits > SHIFT
              y = w_other.bigval
              if opname in COMMUTATIVE_OPS: # add, mul
                z = _rbigint_maskoff_high( liop(y, x), w_other.nbits )
                return W_Bits( w_other.nbits, 0, z )
              else: # sub, should AND get_long_mask
                z = llop( rbigint.fromint(x), y )
                z = z.and_( get_long_mask(w_other.nbits) )
                return W_Bits( w_other.nbits, 0, z )

          elif isinstance(w_other, W_IntObject):
            y = w_other.intval
            mask = get_int_mask(self.nbits)
            try:
              z = ovfcheck( iiop(x, y) )
              return W_Bits( self.nbits, z & mask )
            except OverflowError:
              z = liop( rbigint.fromint(x), y )
              if opname in COMMUTATIVE_OPS: # add, mul
                z = z.digit(0) & mask
              else: # sub, should AND mask
                z = z.int_and_( mask ).digit(0)
              return W_Bits( self.nbits, z )

          elif type(w_other) is W_LongObject:
            y = w_other.num
            mask = get_int_mask(self.nbits)
            if opname in COMMUTATIVE_OPS: # add, mul
              z = liop(y, x).int_and_( mask )
              return W_Bits( self.nbits, z.digit(0) )
            else: # sub
              z = llop( rbigint.fromint(x), y ).int_and_( mask )
              return W_Bits( self.nbits, z.digit(0) )

        # self.nbits > SHIFT, use bigval
        else: # res_nbits > SHIFT
          x = self.bigval
          if isinstance(w_other, W_Bits):
            if w_other.nbits <= SHIFT:
              z = liop( x, w_other.intval )
              if opname == "sub": z = z.and_( get_long_mask(self.nbits) )
              else:               z = _rbigint_maskoff_high( z, self.nbits )
              return W_Bits( self.nbits, 0, z )
            else:
              z = llop( x, w_other.bigval )
              res_nbits = max(self.nbits, w_other.nbits)
              if opname == "sub": z = z.and_( get_long_mask(res_nbits) )
              else:               z = _rbigint_maskoff_high( z, res_nbits )
              return W_Bits( res_nbits, 0, z )

          elif isinstance(w_other, W_IntObject):
            z = liop( x, w_other.intval )
            if opname == "sub": z = z.and_( get_long_mask(self.nbits) )
            else:               z = _rbigint_maskoff_high( z, self.nbits )
            return W_Bits( self.nbits, 0, z )

          elif type(w_other) is W_LongObject:
            z = llop( x, w_other.num )
            if opname == "sub": z = z.and_( get_long_mask(self.nbits) )
            else:               z = _rbigint_maskoff_high( z, self.nbits )
            return W_Bits( self.nbits, 0, z )

      # and, or, xor, no overflow
      # opname should be in COMMUTATIVE_OPS
      else:
        if self.nbits <= SHIFT:
          x = self.intval
          if isinstance(w_other, W_Bits):
            if w_other.nbits <= SHIFT:
              return W_Bits( max(self.nbits, w_other.nbits), iiop( x, w_other.intval ) )
            else:
              return W_Bits( w_other.nbits, 0, liop( w_other.bigval, x ) )
          elif isinstance(w_other, W_IntObject): # TODO Maybe add int_bit_length check?
            return W_Bits( self.nbits, iiop( x, w_other.intval ) )
          elif type(w_other) is W_LongObject: # TODO Maybe add int_bit_length check?
            return W_Bits( self.nbits, 0, liop( w_other.num, x ) )

        # self.nbits > SHIFT, use bigval
        else:
          x = self.bigval
          if isinstance(w_other, W_Bits):
            if w_other.nbits <= SHIFT:
              return W_Bits( self.nbits, 0, liop( x, w_other.intval ) )
            else:
              return W_Bits( max(self.nbits, w_other.nbits), 0, llop( x, w_other.bigval ) )
          elif isinstance(w_other, W_IntObject):
            # TODO Maybe add int_bit_length check?
            return W_Bits( self.nbits, 0, liop( x, w_other.intval ) )
          elif type(w_other) is W_LongObject:
            # TODO Maybe add int_bit_length check?
            return W_Bits( self.nbits, 0, llop( x, w_other.num ) )

      raise oefmt(space.w_TypeError, "Please do %s between Bits and Bits/int/long objects", opname)

    if opname in COMMUTATIVE_OPS:
      @func_renamer('descr_r' + opname)
      def descr_rbinop(self, space, w_other):
        return descr_binop(self, space, w_other)
      return descr_binop, descr_rbinop

    # TODO sub
    @func_renamer('descr_r' + opname)
    def descr_rbinop(self, space, w_other):
      raise oefmt(space.w_TypeError, "r%s not implemented", opname )
    return descr_binop, descr_rbinop

  # Special rsub ..
  def descr_rsub( self, space, w_other ):
    llop = getattr( rbigint, "sub" )
    liop = getattr( rbigint, "int_sub" )
    iiop = getattr( operator, "sub" )

    if self.nbits <= SHIFT:
      y = self.intval
      if isinstance(w_other, W_IntObject):
        x = w_other.intval
        mask = get_int_mask(self.nbits)
        try:
          z = ovfcheck( iiop(x, y) )
          return W_Bits( self.nbits, z & mask )
        except OverflowError:
          z = liop( rbigint.fromint(x), y ).int_and_( mask ).digit(0)
          return W_Bits( self.nbits, z )
      elif type(w_other) is W_LongObject:
        x = w_other.num
        z = liop( x, y ).int_and_( get_int_mask(self.nbits) )
        return W_Bits( self.nbits, z.digit(0) )
    else:
      y = self.bigval
      if isinstance(w_other, W_IntObject):
        z = llop( rbigint.fromint(w_other.intval), y )
        z = z.and_( get_long_mask(self.nbits) )
        return W_Bits( self.nbits, 0, z )

      elif type(w_other) is W_LongObject:
        z = llop( w_other.num, y )
        z = z.and_( get_long_mask(self.nbits) )
        return W_Bits( self.nbits, 0, z )

  descr_add, descr_radd = _make_descr_binop_opname('add')
  descr_sub, _          = _make_descr_binop_opname('sub')
  descr_mul, descr_rmul = _make_descr_binop_opname('mul')

  descr_and, descr_rand = _make_descr_binop_opname('and', ovf=False)
  descr_or, descr_ror   = _make_descr_binop_opname('or', ovf=False)
  descr_xor, descr_rxor = _make_descr_binop_opname('xor', ovf=False)

  def descr_rshift(self, space, w_other):

    if self.nbits <= SHIFT:
      x = self.intval
      if isinstance(w_other, W_Bits):
        if w_other.nbits <= SHIFT:
          shamt = w_other.intval
          if shamt <= SHIFT:  return W_Bits( self.nbits, x >> shamt )
          return W_Bits( self.nbits )
        else:
          big = w_other.bigval
          shamt = big.digit(0)
          if big.numdigits() == 1 and shamt <= SHIFT:
            return W_Bits( self.nbits, x >> shamt )
          return W_Bits( self.nbits )

      elif isinstance(w_other, W_IntObject):
        shamt = w_other.intval
        if shamt < 0: raise oefmt( space.w_ValueError, "negative shift amount" )
        if shamt <= SHIFT:  return W_Bits( self.nbits, x >> shamt )
        return W_Bits( self.nbits )

      elif type(w_other) is W_LongObject:
        big = w_other.num
        if big.sign < 0: raise oefmt( space.w_ValueError, "negative shift amount" )
        shamt = big.digit(0)
        if big.numdigits() == 1 and shamt <= SHIFT:
          W_Bits( self.nbits, x >> shamt )
        return W_Bits( self.nbits )

    # self.nbits > SHIFT, use bigval
    else:
      x = self.bigval
      if isinstance(w_other, W_Bits):
        if w_other.nbits <= SHIFT:
          return W_Bits( self.nbits, 0, x.rshift( w_other.intval ) )
        return W_Bits( self.nbits, 0, _rbigint_rshift( x, w_other.bigval ) )

      elif isinstance(w_other, W_IntObject):
        return W_Bits( self.nbits, 0, x.rshift( w_other.intval ) )

      elif type(w_other) is W_LongObject:
        return W_Bits( self.nbits, 0, _rbigint_rshift( x, w_other.num ) )

    raise oefmt(space.w_TypeError, "Please do rshift between <Bits, Bits/int/long> objects" )

  def descr_rrshift(self, space, w_other): # int >> bits, what is nbits??
    raise oefmt(space.w_TypeError, "rrshift not implemented" )

  def descr_lshift(self, space, w_other):

    if self.nbits <= SHIFT:
      x = self.intval

      if isinstance(w_other, W_Bits):
        if w_other.nbits <= SHIFT:
          shamt = w_other.intval
          if shamt >= self.nbits:  return W_Bits( self.nbits )
          return W_Bits( self.nbits, (x & get_int_mask(self.nbits - shamt)) << shamt )
        else:
          big = w_other.bigval
          shamt = big.digit(0)
          if big.numdigits() == 1 and shamt <= self.nbits:
            return W_Bits( self.nbits, (x & get_int_mask(self.nbits - shamt)) << shamt )
          return W_Bits( self.nbits )

      elif isinstance(w_other, W_IntObject):
        shamt = w_other.intval
        if shamt < 0: raise oefmt( space.w_ValueError, "negative shift amount" )
        if shamt >= self.nbits:  return W_Bits( self.nbits )
        return W_Bits( self.nbits, (x & get_int_mask(self.nbits - shamt)) << shamt )

      elif type(w_other) is W_LongObject:
        big = w_other.num
        if big.sign < 0: raise oefmt( space.w_ValueError, "negative shift amount" )
        shamt = big.digit(0)
        if big.numdigits() == 1 and shamt <= self.nbits:
          return W_Bits( self.nbits, (x & get_int_mask(self.nbits - shamt)) << shamt )
        return W_Bits( self.nbits )

    # self.nbits > SHIFT, use bigval
    else:
      x = self.bigval

      if isinstance(w_other, W_Bits):
        if w_other.nbits <= SHIFT:
          shamt = w_other.intval
          return W_Bits( self.nbits, 0, _rbigint_lshift_maskoff( x, shamt, self.nbits ) )
        else:
          shamt = w_other.bigval
          if shamt.numdigits() > 1: return W_Bits( self.nbits, 0, NULLRBIGINT ) # rare
          shamt = shamt.digit(0)
          return W_Bits( self.nbits, 0, _rbigint_lshift_maskoff( x, shamt, self.nbits ) )

      elif isinstance(w_other, W_IntObject):
        return W_Bits( self.nbits, 0, _rbigint_lshift_maskoff( x, w_other.intval, self.nbits ) )

      elif type(w_other) is W_LongObject:
        shamt = w_other.num
        if shamt.numdigits() > 1: return W_Bits( self.nbits, 0, NULLRBIGINT ) # rare
        shamt = shamt.digit(0)
        return W_Bits( self.nbits, 0, _rbigint_lshift_maskoff( x, shamt, self.nbits ) )

    raise oefmt(space.w_TypeError, "Please do lshift between <Bits, Bits/int/long> objects" )

  def descr_rlshift(self, space, w_other): # int << Bits, what is nbits??
    raise oefmt(space.w_TypeError, "rlshift not implemented" )

  #-----------------------------------------------------------------------
  # <<=
  #-----------------------------------------------------------------------

  def _descr_ilshift(self, space, w_other):
    if not isinstance(w_other, W_Bits):
      raise oefmt(space.w_TypeError, "RHS of <<= has to be Bits, not '%T'", w_other)

    if self.nbits != w_other.nbits:
      raise oefmt(space.w_ValueError, "Bitwidth mismatch Bits%d <> Bits%d",
                                      self.nbits, w_other.nbits)

    if self.nbits <= SHIFT:
      next_intval = w_other.intval
      next_bigval = None
      _bigval = None
    else:
      next_intval = 0
      next_bigval = _rbigint_maskoff_high(w_other.bigval, self.nbits)
      _bigval = self.bigval

    return W_BitsWithNext( self.nbits, self.intval, _bigval,
                           next_intval, next_bigval )


  def descr_ilshift(self, space, w_other):
    return self._descr_ilshift(space, w_other)

  def _descr_flip(self, space):
    raise oefmt(space.w_TypeError, "_flip cannot be called on '%T' objects which has no _next", self)

  def descr_flip(self, space):
    return self._descr_flip(space)

  #-----------------------------------------------------------------------
  # value access
  #-----------------------------------------------------------------------

  def descr_uint(self, space):
    if self.nbits <= SHIFT:
      return wrapint( space, self.intval )
    else:
      return newlong( space, self.bigval )

  def descr_int(self, space): # TODO
    index = self.nbits - 1
    if self.nbits <= SHIFT:
      intval = self.intval
      msb = (intval >> index) & 1
      # if not msb: return wrapint( space, intval )
      # return wrapint( space, intval - get_int_mask(self.nbits) - 1 )
      return wrapint( space, intval - msb*get_int_mask(self.nbits) - msb )

    else:
      bigval = self.bigval
      wordpos = index / SHIFT
      if wordpos > bigval.numdigits(): # msb must be zero, number is positive
        return self.descr_uint( space )

      bitpos = index - wordpos*SHIFT
      word = bigval.digit( wordpos )
      msb = (word >> bitpos) & 1
      if not msb:
        return newlong( space, bigval )

      # calculate self.nbits's index
      bitpos += 1
      if bitpos == SHIFT:
        wordpos += 1
        bitpos = 0

      # manually assemble (1 << (index+1))
      shift = rbigint( [NULLDIGIT]*wordpos + [_store_digit(1 << bitpos)],
                       1, wordpos+1 )

      res = bigval.sub( shift )
      return newlong( space, res )

    raise oefmt(space.w_TypeError, "Bug detected in Bits!" )

  descr_pos = func_with_new_name( descr_uint, 'descr_pos' )
  descr_index = func_with_new_name( descr_uint, 'descr_index' )

  def descr_long(self, space):
    if self.nbits <= SHIFT:
      return wrapint( space, self.intval )
    return newlong( space, self.bigval )

  #-----------------------------------------------------------------------
  # unary ops
  #-----------------------------------------------------------------------

  def descr_bool(self, space):
    if self.nbits <= SHIFT:
      return space.newbool( self.intval != 0 )
    else:
      return space.newbool( self.bigval.sign != 0 )

  def descr_invert(self, space):
    if self.nbits <= SHIFT:
      return W_Bits( self.nbits, get_int_mask(self.nbits) - self.intval )
    inv = get_long_mask(self.nbits).sub( self.bigval )
    return W_Bits( self.nbits, 0, inv )

  # def descr_neg(self, space):

  def descr_hash(self, space):
    hash_nbits = _hash_int( self.nbits )
    hash_value = _hash_int( self.intval )
    if self.nbits > SHIFT:
      hash_value = _hash_long( space, self.bigval )

    # Manually implement a single iter of W_TupleObject.descr_hash

    x = 0x345678
    x = (x ^ hash_nbits) * 1000003
    x = (x ^ hash_value) * (1000003+82520+1+1)
    x += 97531
    return space.newint( intmask(x) )

#-----------------------------------------------------------------------
# Bits with next fields
#-----------------------------------------------------------------------

class W_BitsWithNext(W_Bits):
  __slots__ = ( "nbits", "intval", "bigval", "next_intval", "next_bigval" )
  _immutable_fields_ = [ "nbits" ]

  def __init__( self, nbits, intval=0, bigval=None,
                next_intval=0, next_bigval=None):
    self.nbits  = nbits
    self.intval = intval
    self.bigval = bigval
    self.next_intval = next_intval
    self.next_bigval = next_bigval

  def descr_copy(self):
    return W_BitsWithNext( self.nbits, self.intval, self.bigval,
                           self.next_intval, self.next_bigval )

  def descr_deepcopy(self, w_memo):
    return self.descr_copy()

  def descr_clone(self):
    return self.descr_copy()

  def _descr_ilshift(self, space, w_other):
    if not isinstance(w_other, W_Bits):
      raise oefmt(space.w_TypeError, "RHS of <<= has to be Bits, not '%T'", w_other)

    if self.nbits != w_other.nbits:
      raise oefmt(space.w_ValueError, "Bitwidth mismatch Bits%d <> Bits%d",
                                      self.nbits, w_other.nbits)

    if self.nbits <= SHIFT:
      self.next_intval = w_other.intval
    else:
      # create a new rbigint
      self.next_bigval = _rbigint_maskoff_high(w_other.bigval, self.nbits)

    return self

  def _descr_flip(self, space):
    if self.nbits <= SHIFT:
      self.intval = self.next_intval
    else:
      self.bigval = _rbigint_maskoff_high(self.next_bigval, self.nbits)




W_Bits.typedef = TypeDef("Bits",
    nbits = GetSetProperty(W_Bits.descr_get_nbits),

    uint  = interp2app(W_Bits.descr_uint),
    int   = interp2app(W_Bits.descr_int),

    # Basic operations
    __new__ = interp2app(W_Bits.descr_new),
    __getitem__ = interpindirect2app(W_Bits.descr_getitem),
    __setitem__ = interpindirect2app(W_Bits.descr_setitem),
    __copy__ = interpindirect2app(W_Bits.descr_copy),
    __deepcopy__ = interpindirect2app(W_Bits.descr_deepcopy),

    # String formats
    __oct__  = interpindirect2app(W_Bits.descr_oct),
    __hex__  = interpindirect2app(W_Bits.descr_hex),
    __repr__ = interpindirect2app(W_Bits.descr_repr),
    __str__  = interpindirect2app(W_Bits.descr_str),

    # Comparators
    __lt__ = interpindirect2app(W_Bits.descr_lt),
    __le__ = interpindirect2app(W_Bits.descr_le),
    __eq__ = interpindirect2app(W_Bits.descr_eq),
    __ne__ = interpindirect2app(W_Bits.descr_ne),
    __gt__ = interpindirect2app(W_Bits.descr_gt),
    __ge__ = interpindirect2app(W_Bits.descr_ge),

    # Value access
    __int__   = interpindirect2app(W_Bits.descr_uint), # TODO use uint now
    __pos__   = interpindirect2app(W_Bits.descr_pos),
    __index__ = interpindirect2app(W_Bits.descr_index),
    __long__  = interpindirect2app(W_Bits.descr_long),

    # Unary ops
    # __neg__     = interpindirect2app(W_Bits.descr_neg),
    # __abs__     = interpindirect2app(W_Bits.descr_abs),
    __bool__   = interpindirect2app(W_Bits.descr_bool), # no __nonzero__ in Python3 anymore
    __invert__ = interpindirect2app(W_Bits.descr_invert),
    __hash__   = interpindirect2app(W_Bits.descr_hash),

    # Binary fast arith ops
    __add__  = interpindirect2app(W_Bits.descr_add),
    __radd__ = interpindirect2app(W_Bits.descr_radd),
    __sub__  = interpindirect2app(W_Bits.descr_sub),
    __rsub__ = interpindirect2app(W_Bits.descr_rsub),
    __mul__  = interpindirect2app(W_Bits.descr_mul),
    __rmul__ = interpindirect2app(W_Bits.descr_rmul),

    # Binary logic ops
    __and__  = interpindirect2app(W_Bits.descr_and),
    __rand__ = interpindirect2app(W_Bits.descr_rand),
    __or__   = interpindirect2app(W_Bits.descr_or),
    __ror__  = interpindirect2app(W_Bits.descr_ror),
    __xor__  = interpindirect2app(W_Bits.descr_xor),
    __rxor__ = interpindirect2app(W_Bits.descr_rxor),

    # Binary shift ops
    __lshift__  = interpindirect2app(W_Bits.descr_lshift),
    __rlshift__ = interpindirect2app(W_Bits.descr_rlshift),
    __rshift__  = interpindirect2app(W_Bits.descr_rshift),
    __rrshift__ = interpindirect2app(W_Bits.descr_rrshift),

    # <<=
    __ilshift__ = interpindirect2app(W_Bits.descr_ilshift),
    _flip = interpindirect2app(W_Bits.descr_flip),

    # clone
    clone = interpindirect2app(W_Bits.descr_clone),

    # Binary slow arith ops
    # __floordiv__  = interpindirect2app(W_Bits.descr_floordiv),
    # __rfloordiv__ = interpindirect2app(W_Bits.descr_rfloordiv),
    # __div__       = interpindirect2app(W_Bits.descr_div),
    # __rdiv__      = interpindirect2app(W_Bits.descr_rdiv),
    # __truediv__   = interpindirect2app(W_Bits.descr_truediv),
    # __rtruediv__  = interpindirect2app(W_Bits.descr_rtruediv),
    # __mod__       = interpindirect2app(W_Bits.descr_mod),
    # __rmod__      = interpindirect2app(W_Bits.descr_rmod),
    # __divmod__    = interpindirect2app(W_Bits.descr_divmod),
    # __rdivmod__   = interpindirect2app(W_Bits.descr_rdivmod),
    # __pow__       = interpindirect2app(W_Bits.descr_pow),
    # __rpow__      = interpindirect2app(W_Bits.descr_rpow),
)